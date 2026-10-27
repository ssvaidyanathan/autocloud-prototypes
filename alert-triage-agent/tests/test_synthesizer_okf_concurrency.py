"""Concurrency and field-preservation tests for the synthesizer's OKF update.

``SynthesizerAgent._update_okf_file`` runs inside the timer-wake investigation while the
triage queue (duplicate append, attach, close) and other timer wakes (ticket linkage) write
the same incident group. These tests use the generation-enforcing GCS mock to verify that:

1. Fields the synthesizer does not own (deadlines, ticket URL, degraded/suppressed flags,
   unknown keys, duplicate notes in the body) survive the update.
2. A timer ``_mark_ticketed`` write racing the synthesizer update is not lost.
3. A 412 is retried against a fresh read.
4. Storage failures, including exhausted retries, are logged at ERROR with the group id and
   object path (no content) and propagate.
5. Through ``/timer/wake`` a failed OKF update is visible at ERROR and the caller's existing
   contract (fallback report, ticket still minted) applies.

In production the incident store path is the GCS FUSE mount of the incidents bucket, so a
``Path.write_text`` on it is an unconditional upload of the same object. ``FuseShim`` models
that, so the old write path is exercised against the same mock object as the new one.
"""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from google.api_core import exceptions
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import TicketResponse
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.models import OKFDocument
from agent.app.grouping.store import ConcurrencyLockError, OKFStore
from agent.app.main import app, set_clock_func
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.timers.scheduler import set_timer_scheduler_hook
from agent.app.triage import generate_investigation_report
from tests.test_agent_grouping_and_timers import (
    ConcurrencyMockBlob,
    ConcurrencyMockStorageClient,
)

BUCKET = "test-incidents"
GROUP = "ata-inventory-5xx-ata-inventory-api-synth"
FILENAME = f"incident-{GROUP}.md"
BLOB = f"knowledge/incident-manager/{FILENAME}"
DUP_NOTE = "**Duplicate Alert Appended**\n- Incident ID: `0.dup`\n- State: `open`"
REPORT_HEADING = "# Incident Report for"

# Fields owned by other writers that the synthesizer must carry over untouched.
PRESERVED = {
    "status": "investigating",
    "created_at": "2026-10-27T03:00:00Z",
    "updated_at": "2026-10-27T03:02:00Z",
    "settle_deadline": "2026-10-27T03:02:00Z",
    "max_wait_deadline": "2026-10-27T03:15:00Z",
    "cooling_deadline": "2026-10-27T03:20:00Z",
    "servicenow_url": "https://sn.example/sys_prior",
    "degraded": True,
    "degraded_reason": "bridge unavailable",
    "suppressed": True,
    "suppression_reason": "maintenance window",
    "timestamp": "2026-10-27T03:00:00Z",
}


@pytest.fixture(autouse=True)
def reset_globals():
    set_clock_func(None)
    set_timer_scheduler_hook(lambda **kwargs: f"mock-task-{kwargs.get('deadline_type')}")
    yield
    set_clock_func(None)
    set_timer_scheduler_hook(None)
    app.dependency_overrides.clear()


@pytest.fixture
def cloud_cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        project_id="p",
        name_prefix="ata",
        incidents_bucket=BUCKET,
        incident_store_path=str(tmp_path),
        fallback_store_path=str(tmp_path),
        bridge_url="http://mock-bridge:8080",
        agent_url="http://mock-agent:8000",
        is_local=False,
    )


@pytest.fixture
def local_cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        project_id="p",
        name_prefix="ata",
        incident_store_path=str(tmp_path),
        fallback_store_path=str(tmp_path),
        is_local=True,
    )


@pytest.fixture
def gcs() -> ConcurrencyMockStorageClient:
    return ConcurrencyMockStorageClient()


class FuseShim:
    """Route Path I/O on the mount directory to the mock bucket, like GCS FUSE does.

    A FUSE write is an upload with no generation precondition.
    """

    def __init__(self, mount: Path, gcs: ConcurrencyMockStorageClient) -> None:
        self.mount = mount
        self.gcs = gcs
        self.writes: list[str] = []

    def _blob(self, path: Path) -> ConcurrencyMockBlob | None:
        if path.parent != self.mount or path.name.startswith("."):
            return None
        return self.gcs.bucket(BUCKET).blob(f"knowledge/incident-manager/{path.name}")

    def install(self):
        shim = self
        real_exists, real_read, real_write = Path.exists, Path.read_text, Path.write_text

        def exists(self, *a, **kw):
            blob = shim._blob(self)
            return blob._exists if blob is not None else real_exists(self, *a, **kw)

        def read_text(self, *a, **kw):
            blob = shim._blob(self)
            return blob.download_as_text() if blob is not None else real_read(self, *a, **kw)

        def write_text(self, data, *a, **kw):
            blob = shim._blob(self)
            if blob is None:
                return real_write(self, data, *a, **kw)
            shim.writes.append(self.name)
            blob.upload_from_string(data, content_type="text/markdown")
            return len(data)

        return (
            patch.object(Path, "exists", exists),
            patch.object(Path, "read_text", read_text),
            patch.object(Path, "write_text", write_text),
        )


class cloud_env:
    """Patch the GCS client and mount the FUSE shim for the duration of a block."""

    def __init__(self, tmp_path: Path, gcs: ConcurrencyMockStorageClient) -> None:
        self.shim = FuseShim(tmp_path, gcs)
        self._patches = [patch("google.cloud.storage.Client", return_value=gcs), *self.shim.install()]

    def __enter__(self) -> FuseShim:
        for p in self._patches:
            p.start()
        return self.shim

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()


def _seed_doc() -> OKFDocument:
    return OKFDocument(
        group_id=GROUP,
        project="p",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        alerts=[{
            "incident_id": "0.first",
            "policy_name": "ata-inventory-5xx",
            "resource_name": "ata-inventory-api",
            "state": "open",
            "started_at": "2026-10-27T03:00:00Z",
        }],
        custom_annotation="kept-by-extra-allow",
        body=f"Prior notes.\n\n{DUP_NOTE}",
        **PRESERVED,
    )


def _seed_gcs(gcs: ConcurrencyMockStorageClient) -> OKFStore:
    store = OKFStore(bucket_name=BUCKET, storage_client=gcs, is_local=False)
    store.write(GROUP, _seed_doc())
    return store


def _blob(gcs: ConcurrencyMockStorageClient) -> ConcurrencyMockBlob:
    return gcs.bucket(BUCKET).blob(BLOB)


def _final(gcs: ConcurrencyMockStorageClient) -> OKFDocument:
    return OKFDocument.from_markdown(_blob(gcs).content)


def _alert(incident_id: str = "0.synth") -> AlertEvent:
    return AlertEvent(
        incident_id=incident_id,
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-27T03:05:00Z",
    )


def _report(alert: AlertEvent, cfg: AgentConfig):
    report = generate_investigation_report(alert, GROUP, FILENAME, cfg)
    report.group_id = GROUP
    report.filename = FILENAME
    return report


def _mark_ticketed(doc: OKFDocument) -> OKFDocument:
    doc.servicenow_sys_id = "sys_123"
    doc.servicenow_number = "INC0010001"
    doc.status = "ticketed"
    return doc


def _assert_synthesized_and_preserved(final: OKFDocument, report) -> None:
    # Synthesizer-owned fields are written.
    assert final.origin == report.origin
    assert report.origin in final.blast_radius
    assert final.priority == report.priority
    assert final.confidence == report.probable_root_cause.confidence
    assert {a["incident_id"] for a in final.alerts} == {"0.first", "0.synth"}
    assert REPORT_HEADING in final.body
    # Everything else is carried over.
    for key, value in PRESERVED.items():
        actual = getattr(final, key)
        actual = actual.value if hasattr(actual, "value") else actual
        assert actual == value, key
    assert final.custom_annotation == "kept-by-extra-allow"
    assert final.policy_name == "ata-inventory-5xx"
    assert final.resource_name == "ata-inventory-api"
    assert final.servicenow_sys_id is None
    assert DUP_NOTE in final.body
    assert "Prior notes." in final.body


def _synth_errors(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "agent.app.specialists.synthesizer"
    ]


# ------------------------------------------------------------------------------
# 1. Field preservation
# ------------------------------------------------------------------------------

def test_synth_update_preserves_unowned_fields_gcs(tmp_path, gcs, cloud_cfg):
    _seed_gcs(gcs)
    alert = _alert()
    report = _report(alert, cloud_cfg)
    uploads: list[int | None] = []
    real_upload = ConcurrencyMockBlob.upload_from_string

    def recording_upload(self, data, content_type="text/markdown", if_generation_match=None):
        uploads.append(if_generation_match)
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    with cloud_env(tmp_path, gcs) as fuse, patch.object(ConcurrencyMockBlob, "upload_from_string", recording_upload):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    _assert_synthesized_and_preserved(_final(gcs), report)
    # Only a conditional GCS API write; nothing written through the FUSE mount.
    assert fuse.writes == []
    assert uploads == [1]


def test_synth_update_preserves_unowned_fields_local(tmp_path, local_cfg):
    store = OKFStore(storage_path=tmp_path, config=local_cfg, is_local=True)
    store.write(GROUP, _seed_doc())
    alert = _alert()
    report = _report(alert, local_cfg)

    SynthesizerAgent()._update_okf_file(report, alert, local_cfg)

    _assert_synthesized_and_preserved(OKFDocument.from_markdown((tmp_path / FILENAME).read_text()), report)


def test_synth_rerun_replaces_its_report_section_only(tmp_path, local_cfg):
    store = OKFStore(storage_path=tmp_path, config=local_cfg, is_local=True)
    store.write(GROUP, _seed_doc())
    alert = _alert()
    report = _report(alert, local_cfg)

    SynthesizerAgent()._update_okf_file(report, alert, local_cfg)
    SynthesizerAgent()._update_okf_file(report, alert, local_cfg)

    body = OKFDocument.from_markdown((tmp_path / FILENAME).read_text()).body
    assert body.count(REPORT_HEADING) == 1
    assert body.count(DUP_NOTE) == 1


# ------------------------------------------------------------------------------
# 2. Lost update: a timer ticket write lands between the synthesizer's read and write
# ------------------------------------------------------------------------------

def test_timer_ticket_write_racing_synth_update_survives(tmp_path, gcs, cloud_cfg):
    store = _seed_gcs(gcs)
    alert = _alert()
    report = _report(alert, cloud_cfg)
    real_upload = ConcurrencyMockBlob.upload_from_string
    raced = {"done": False}

    def racing_upload(self, data, content_type="text/markdown", if_generation_match=None):
        if not raced["done"]:
            raced["done"] = True
            store.update_atomically(GROUP, _mark_ticketed)
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    with cloud_env(tmp_path, gcs), patch.object(ConcurrencyMockBlob, "upload_from_string", racing_upload):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    final = _final(gcs)
    assert final.servicenow_sys_id == "sys_123"
    assert final.servicenow_number == "INC0010001"
    assert str(final.status.value if hasattr(final.status, "value") else final.status) == "ticketed"
    assert final.origin == report.origin
    assert REPORT_HEADING in final.body
    assert DUP_NOTE in final.body
    assert _blob(gcs).generation == 3


# ------------------------------------------------------------------------------
# 3. 412 then success
# ------------------------------------------------------------------------------

def test_synth_update_retries_after_412(tmp_path, gcs, cloud_cfg):
    _seed_gcs(gcs)
    alert = _alert()
    report = _report(alert, cloud_cfg)
    blob = _blob(gcs)
    attempts: list[int | None] = []
    real_upload = ConcurrencyMockBlob.upload_from_string

    def upload_412_once(self, data, content_type="text/markdown", if_generation_match=None):
        attempts.append(if_generation_match)
        if len(attempts) == 1:
            # Another writer commits a cooling deadline first, then this write gets a 412.
            concurrent = OKFDocument.from_markdown(self.content)
            concurrent.cooling_deadline = "2026-10-27T04:00:00Z"
            concurrent.servicenow_sys_id = "sys_concurrent"
            real_upload(self, concurrent.to_markdown(), if_generation_match=self.generation)
            raise exceptions.PreconditionFailed("generation mismatch")
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    with cloud_env(tmp_path, gcs), patch.object(ConcurrencyMockBlob, "upload_from_string", upload_412_once), \
         patch("agent.app.grouping.store.time.sleep"):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    assert attempts == [1, 2]
    final = _final(gcs)
    assert final.cooling_deadline == "2026-10-27T04:00:00Z"
    assert final.servicenow_sys_id == "sys_concurrent"
    assert final.origin == report.origin
    assert REPORT_HEADING in final.body
    assert blob.generation == 3


# ------------------------------------------------------------------------------
# 4. Errors are logged at ERROR and propagate
# ------------------------------------------------------------------------------

def test_synth_update_raises_when_retries_exhausted(tmp_path, gcs, cloud_cfg, caplog):
    _seed_gcs(gcs)
    alert = _alert()
    report = _report(alert, cloud_cfg)
    before = _blob(gcs).content

    def always_412(self, data, content_type="text/markdown", if_generation_match=None):
        raise exceptions.PreconditionFailed("generation mismatch")

    caplog.set_level(logging.WARNING)
    with cloud_env(tmp_path, gcs), patch.object(ConcurrencyMockBlob, "upload_from_string", always_412), \
         patch("agent.app.grouping.store.time.sleep"), pytest.raises(ConcurrencyLockError):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    assert _blob(gcs).content == before
    errors = _synth_errors(caplog)
    assert len(errors) == 1
    assert GROUP in errors[0]
    assert f"gs://{BUCKET}/{BLOB}" in errors[0]
    assert "ConcurrencyLockError" in errors[0]
    assert "Duplicate Alert Appended" not in errors[0]


def test_synth_update_propagates_storage_errors(tmp_path, gcs, cloud_cfg, caplog):
    _seed_gcs(gcs)
    alert = _alert()
    report = _report(alert, cloud_cfg)
    before = _blob(gcs).content

    def unavailable(self, data, content_type="text/markdown", if_generation_match=None):
        raise exceptions.ServiceUnavailable("gcs down")

    with cloud_env(tmp_path, gcs), patch.object(ConcurrencyMockBlob, "upload_from_string", unavailable), \
         pytest.raises(exceptions.ServiceUnavailable):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    assert _blob(gcs).content == before
    errors = _synth_errors(caplog)
    assert len(errors) == 1
    assert GROUP in errors[0] and f"gs://{BUCKET}/{BLOB}" in errors[0]


def test_synth_update_logs_unparseable_target_without_content(tmp_path, gcs, cloud_cfg, caplog):
    blob = _blob(gcs)
    secret_body = "---\ngroup_id: [unterminated secret-token-xyz\n---\nbody"
    blob.upload_from_string(secret_body)
    alert = _alert()
    report = _report(alert, cloud_cfg)

    with cloud_env(tmp_path, gcs), pytest.raises(yaml.YAMLError):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    assert blob.content == secret_body
    errors = _synth_errors(caplog)
    assert len(errors) == 1
    assert GROUP in errors[0] and f"gs://{BUCKET}/{BLOB}" in errors[0]
    assert "secret-token-xyz" not in errors[0]


# ------------------------------------------------------------------------------
# 5. Caller contract through /timer/wake
# ------------------------------------------------------------------------------

def test_timer_wake_surfaces_okf_update_failure(tmp_path, gcs, cloud_cfg, monkeypatch, caplog):
    """A failed synthesizer OKF update is logged at ERROR (group, object path) and the timer
    wake follows its existing contract: fallback report, ticket minted, other fields intact."""
    seeded = _seed_doc()
    seeded.status = "open"
    seeded.settle_deadline = "2026-10-27T03:02:00Z"
    OKFStore(bucket_name=BUCKET, storage_client=gcs, is_local=False).write(GROUP, seeded)

    async def synthesize_then_write(alert, group_id, filename, config):
        # Stand-in for SynthesizerAgent.execute_investigation without live specialists:
        # same tail call into the real _update_okf_file.
        report = generate_investigation_report(alert, group_id, filename, config)
        SynthesizerAgent()._update_okf_file(report, alert, config)
        return report

    fallback_calls: list[str] = []
    real_fallback = agent_main.generate_investigation_report

    def spy_fallback(*args, **kwargs):
        fallback_calls.append(args[1])
        return real_fallback(*args, **kwargs)

    tickets: list[str] = []

    def mock_create_ticket(self, bridge_url, request, config=None, http_client=None):
        tickets.append(request.group_id)
        return TicketResponse(status="created", sys_id="sys_tw", number="INC0020002",
                              url="https://sn.example/sys_tw", group_id=request.group_id)

    real_upload = ConcurrencyMockBlob.upload_from_string

    def fail_report_writes(self, data, content_type="text/markdown", if_generation_match=None):
        if REPORT_HEADING in data:
            raise exceptions.ServiceUnavailable("gcs down")
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", synthesize_then_write)
    monkeypatch.setattr(agent_main, "generate_investigation_report", spy_fallback)
    monkeypatch.setattr(BridgeClient, "create_ticket", mock_create_ticket)
    app.dependency_overrides[get_agent_settings] = lambda: cloud_cfg
    set_clock_func(lambda: 1_900_000_000.0)

    with cloud_env(tmp_path, gcs), patch.object(ConcurrencyMockBlob, "upload_from_string", fail_report_writes):
        res = TestClient(app).post(
            "/timer/wake",
            json=TimerWakeRequest(group_id=GROUP, deadline_type="settle", scheduled_epoch=1).model_dump(mode="json"),
        )

    assert res.status_code == 200
    assert res.json()["status"] == "executed"
    assert fallback_calls == [GROUP]
    assert tickets == [GROUP]
    errors = _synth_errors(caplog)
    assert len(errors) == 1
    assert GROUP in errors[0] and f"gs://{BUCKET}/{BLOB}" in errors[0]
    final = _final(gcs)
    assert final.servicenow_sys_id == "sys_tw"
    assert final.max_wait_deadline == PRESERVED["max_wait_deadline"]
    assert DUP_NOTE in final.body
