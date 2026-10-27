"""Concurrency and field-preservation tests for the synthesizer's OKF update.

``SynthesizerAgent._update_okf_file`` runs inside the timer-wake investigation while the
triage queue (duplicate append, attach, close) and other timer wakes (ticket linkage) write
the same incident group. These tests use the generation-enforcing GCS mock to verify that:

1. Fields the synthesizer does not own (deadlines, ticket URL, degraded/suppressed flags,
   unknown keys, duplicate notes in the body) survive the update.
2. A timer ``_mark_ticketed`` write racing the synthesizer update is not lost.
3. A 412 is retried against a fresh read.
4. Storage failures, including exhausted retries, are logged at ERROR with the group id and
   object path (no content) and propagate out of ``_update_okf_file``.
5. ``execute_investigation`` does not let a persistence-only failure discard the finished
   investigation: it returns the synthesized report stamped with a visible ``DEGRADED MODE``
   note, and that note reaches the ServiceNow work note and description. Through every caller
   (``/timer/wake`` settle and max-wait, ``/investigate``) the ticket or callback carries the
   synthesized report, not the fallback.
6. When the investigation itself fails, the callers' fallback report is marked degraded and the
   caller logs the group id and exception type at ERROR, never the exception text.

In production the incident store path is the GCS FUSE mount of the incidents bucket, so a
``Path.write_text`` on it is an unconditional upload of the same object. ``FuseShim`` models
that, so the old write path is exercised against the same mock object as the new one.
"""

import asyncio
import logging
import os
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
from agent.app.grouping import store as store_module
from agent.app.grouping.models import OKFDocument
from agent.app.grouping.store import ConcurrencyLockError, OKFStore
from agent.app.main import app, set_clock_func
from agent.app.specialists import synthesizer as synth_module
from agent.app.specialists.apigee import ApigeeSpecialist
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.change import ChangeSpecialist
from agent.app.specialists.cloud_run import CloudRunSpecialist
from agent.app.specialists.cloud_sql import CloudSQLSpecialist
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.timers.scheduler import set_timer_scheduler_hook
from agent.app.triage import generate_investigation_report
from bridge.app.contracts.report import InvestigationReport as BridgeInvestigationReport
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
# 4b. Topology I/O and origin resolution across precondition retries
# ------------------------------------------------------------------------------

def _upload_412_once_after(change):
    """Patch uploads so the first one loses to a concurrent writer applying ``change``."""
    attempts: list[int | None] = []
    real_upload = ConcurrencyMockBlob.upload_from_string

    def upload(self, data, content_type="text/markdown", if_generation_match=None):
        attempts.append(if_generation_match)
        if len(attempts) == 1:
            concurrent = OKFDocument.from_markdown(self.content)
            change(concurrent)
            real_upload(self, concurrent.to_markdown(), if_generation_match=self.generation)
            raise exceptions.PreconditionFailed("generation mismatch")
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    return attempts, patch.object(ConcurrencyMockBlob, "upload_from_string", upload)


def test_synth_update_retry_does_not_repeat_topology_lookups(tmp_path, gcs, cloud_cfg, monkeypatch):
    _seed_gcs(gcs)
    alert = _alert()
    report = _report(alert, cloud_cfg)
    calls = {"get_blast_radius": 0, "get_loader": 0}
    for name in calls:
        real = getattr(synth_module, name)

        def counting(*a, _real=real, _name=name, **kw):
            calls[_name] += 1
            return _real(*a, **kw)

        monkeypatch.setattr(synth_module, name, counting)
    attempts, upload_patch = _upload_412_once_after(lambda d: setattr(d, "cooling_deadline", "2026-10-27T04:00:00Z"))

    with cloud_env(tmp_path, gcs), upload_patch, patch("agent.app.grouping.store.time.sleep"):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    assert attempts == [1, 2]
    assert calls == {"get_blast_radius": 1, "get_loader": 1}
    final = _final(gcs)
    assert final.cooling_deadline == "2026-10-27T04:00:00Z"
    assert REPORT_HEADING in final.body


def test_synth_update_without_report_origin_uses_origin_stored_by_concurrent_writer(tmp_path, gcs, cloud_cfg):
    seeded = _seed_doc()
    seeded.origin = "ata-inventory-db"
    OKFStore(bucket_name=BUCKET, storage_client=gcs, is_local=False).write(GROUP, seeded)
    alert = _alert()
    report = _report(alert, cloud_cfg)
    report.origin = ""
    attempts, upload_patch = _upload_412_once_after(lambda d: setattr(d, "origin", "ata-orders-api"))

    with cloud_env(tmp_path, gcs), upload_patch, patch("agent.app.grouping.store.time.sleep"):
        SynthesizerAgent()._update_okf_file(report, alert, cloud_cfg)

    assert attempts == [1, 2]
    assert _final(gcs).origin == "ata-orders-api"
    assert report.origin == "ata-orders-api"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_local_okf_log_names_fallback_path_when_primary_dir_cannot_be_created(tmp_path, local_cfg, monkeypatch, caplog):
    locked_parent = tmp_path / "locked"
    locked_parent.mkdir()
    primary = locked_parent / "store"
    fallback_dir = tmp_path / "fallback"
    # The configured primary directory vanished after start-up and cannot be recreated.
    monkeypatch.setattr(AgentConfig, "get_effective_store_path", lambda self: primary)
    monkeypatch.setattr(store_module, "LOCAL_FALLBACK_DIR", fallback_dir)
    alert = _alert()
    report = _report(alert, local_cfg)
    caplog.set_level(logging.INFO, logger="agent.app.specialists.synthesizer")

    locked_parent.chmod(0o500)
    try:
        SynthesizerAgent()._update_okf_file(report, alert, local_cfg)
    finally:
        locked_parent.chmod(0o700)

    assert (fallback_dir / FILENAME).exists()
    updated = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Updated OKF incident file")]
    assert len(updated) == 1
    assert str(fallback_dir / FILENAME) in updated[0]
    assert str(primary) not in updated[0]


# ------------------------------------------------------------------------------
# 5. A persistence-only failure keeps the synthesized report and marks it degraded
# ------------------------------------------------------------------------------

SYNTH_MARKER = "SYNTH-EVIDENCE"
DEGRADED_OKF_NOTE = "DEGRADED MODE: incident OKF file update failed"
TICKET_SYS_ID = "sys_tw"


@pytest.fixture
def stub_specialists(monkeypatch):
    """Deterministic specialists (no live GCP). Every dossier carries ``SYNTH_MARKER``, so
    the synthesized report is distinguishable from the single-pass fallback report."""

    async def investigate(self, alert, config, target_service=None):
        name = type(self).__name__
        return EvidenceDossier(
            specialist_name=name,
            target_resource=target_service or alert.resource_name,
            findings=[f"{SYNTH_MARKER} observed by {name}"],
        )

    for cls in (CloudRunSpecialist, ApigeeSpecialist, CloudSQLSpecialist, ChangeSpecialist):
        monkeypatch.setattr(cls, "investigate", investigate)


def _fail_report_writes(exc: Exception):
    """Fail only the synthesizer's write (the one carrying the report section)."""
    real_upload = ConcurrencyMockBlob.upload_from_string

    def upload(self, data, content_type="text/markdown", if_generation_match=None):
        if REPORT_HEADING in data:
            raise exc
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    return patch.object(ConcurrencyMockBlob, "upload_from_string", upload)


def _servicenow_texts(report) -> dict[str, str]:
    """Render the report the way the bridge does for ServiceNow (it re-parses the JSON)."""
    bridge_report = BridgeInvestigationReport.model_validate(report.model_dump(mode="json"))
    return {
        "work_note": bridge_report.build_executive_summary_html(),
        "description": bridge_report.build_structured_description(project_id="p", incidents_bucket=BUCKET),
    }


def _assert_degraded_synthesized(report, exc_type: str) -> None:
    assert SYNTH_MARKER in report.observations, "ticket must carry the synthesized report"
    assert report.observations.startswith(f"{DEGRADED_OKF_NOTE} ({exc_type})")
    assert f"gs://{BUCKET}/{BLOB}" in report.observations
    for name, text in _servicenow_texts(report).items():
        assert f"{DEGRADED_OKF_NOTE} ({exc_type})" in text, name
        assert SYNTH_MARKER in text, name


@pytest.mark.parametrize(
    ("failure", "exc_type"),
    [
        (exceptions.ServiceUnavailable("gcs down secret-token-xyz"), "ServiceUnavailable"),
        (exceptions.PreconditionFailed("generation mismatch"), "ConcurrencyLockError"),
    ],
    ids=["storage-error", "retries-exhausted"],
)
def test_execute_investigation_returns_synthesized_report_marked_degraded_when_okf_persist_fails(
    tmp_path, gcs, cloud_cfg, stub_specialists, caplog, failure, exc_type
):
    _seed_gcs(gcs)
    before = _blob(gcs).content

    with cloud_env(tmp_path, gcs), _fail_report_writes(failure), patch("agent.app.grouping.store.time.sleep"):
        report = asyncio.run(SynthesizerAgent().execute_investigation(_alert(), GROUP, FILENAME, cloud_cfg))

    _assert_degraded_synthesized(report, exc_type)
    assert "secret-token-xyz" not in report.observations
    assert _blob(gcs).content == before
    errors = _synth_errors(caplog)
    assert len(errors) == 1
    assert GROUP in errors[0] and f"gs://{BUCKET}/{BLOB}" in errors[0] and exc_type in errors[0]


def test_execute_investigation_persisted_report_carries_no_degraded_note(tmp_path, gcs, cloud_cfg, stub_specialists, caplog):
    _seed_gcs(gcs)

    with cloud_env(tmp_path, gcs):
        report = asyncio.run(SynthesizerAgent().execute_investigation(_alert(), GROUP, FILENAME, cloud_cfg))

    assert SYNTH_MARKER in report.observations
    assert "DEGRADED" not in report.observations
    assert "degraded" not in report.summary.lower()
    assert REPORT_HEADING in _final(gcs).body
    assert _synth_errors(caplog) == []


def _seed_for_wake(gcs: ConcurrencyMockStorageClient) -> None:
    seeded = _seed_doc()
    seeded.status = "open"
    OKFStore(bucket_name=BUCKET, storage_client=gcs, is_local=False).write(GROUP, seeded)


def _spy_fallback(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real_fallback = agent_main.generate_investigation_report

    def spy(*args, **kwargs):
        calls.append({"group_id": args[1], "degraded_reason": kwargs.get("degraded_reason")})
        return real_fallback(*args, **kwargs)

    monkeypatch.setattr(agent_main, "generate_investigation_report", spy)
    return calls


def _capture_tickets(monkeypatch) -> list:
    requests: list = []

    def mock_create_ticket(self, bridge_url, request, config=None, http_client=None):
        requests.append(request)
        return TicketResponse(status="created", sys_id=TICKET_SYS_ID, number="INC0020002",
                              url="https://sn.example/sys_tw", group_id=request.group_id)

    monkeypatch.setattr(BridgeClient, "create_ticket", mock_create_ticket)
    return requests


def _post_wake(cloud_cfg: AgentConfig, deadline_type: str):
    app.dependency_overrides[get_agent_settings] = lambda: cloud_cfg
    set_clock_func(lambda: 1_900_000_000.0)
    return TestClient(app).post(
        "/timer/wake",
        json=TimerWakeRequest(group_id=GROUP, deadline_type=deadline_type, scheduled_epoch=1).model_dump(mode="json"),
    )


@pytest.mark.parametrize("deadline_type", ["settle", "max_wait"])
def test_timer_wake_mints_ticket_from_synthesized_report_marked_degraded_when_okf_persist_fails(
    tmp_path, gcs, cloud_cfg, stub_specialists, monkeypatch, caplog, deadline_type
):
    _seed_for_wake(gcs)
    fallback_calls = _spy_fallback(monkeypatch)
    tickets = _capture_tickets(monkeypatch)

    with cloud_env(tmp_path, gcs), _fail_report_writes(exceptions.ServiceUnavailable("gcs down")):
        res = _post_wake(cloud_cfg, deadline_type)

    assert res.status_code == 200
    assert res.json()["status"] == "executed"
    assert fallback_calls == []
    assert len(tickets) == 1
    ticket_report = tickets[0].report
    _assert_degraded_synthesized(ticket_report, "ServiceUnavailable")
    errors = _synth_errors(caplog)
    assert len(errors) == 1
    assert GROUP in errors[0] and f"gs://{BUCKET}/{BLOB}" in errors[0]
    # The ticket linkage still lands and records the synthesized origin and blast radius.
    final = _final(gcs)
    assert final.servicenow_sys_id == TICKET_SYS_ID
    assert final.origin == ticket_report.origin
    assert final.blast_radius == ticket_report.blast_radius
    assert final.max_wait_deadline == PRESERVED["max_wait_deadline"]
    assert DUP_NOTE in final.body
    assert REPORT_HEADING not in final.body


def test_investigate_returns_synthesized_report_marked_degraded_when_okf_persist_fails(
    tmp_path, gcs, cloud_cfg, stub_specialists, monkeypatch, caplog
):
    fallback_calls = _spy_fallback(monkeypatch)
    callbacks: list = []
    monkeypatch.setattr(agent_main, "send_findings_to_bridge",
                        lambda report, bridge_url, config=None: callbacks.append(report) or {"status": "ok"})
    app.dependency_overrides[get_agent_settings] = lambda: cloud_cfg

    with cloud_env(tmp_path, gcs), _fail_report_writes(exceptions.ServiceUnavailable("gcs down")):
        res = TestClient(app).post(
            "/investigate",
            json={"alert": _alert().model_dump(mode="json"), "group_id": GROUP, "filename": FILENAME},
        )

    assert res.status_code == 200
    assert res.json()["status"] == "investigated"
    assert fallback_calls == []
    assert len(callbacks) == 1
    _assert_degraded_synthesized(callbacks[0], "ServiceUnavailable")
    assert res.json()["report"]["observations"] == callbacks[0].observations
    errors = _synth_errors(caplog)
    assert len(errors) == 1
    assert GROUP in errors[0] and f"gs://{BUCKET}/{BLOB}" in errors[0]


# ------------------------------------------------------------------------------
# 6. A failed investigation falls back to a report that is marked degraded
# ------------------------------------------------------------------------------

LEAKY_MESSAGE = "while parsing a block mapping secret-token-xyz"


async def _failing_investigation(alert, group_id, filename, config):
    raise RuntimeError(LEAKY_MESSAGE)


def _main_logs(caplog, min_level: int = logging.WARNING) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "agent.app.main" and r.levelno >= min_level]


def _assert_fallback_marked_degraded(report, fallback_calls: list[dict], caplog) -> None:
    assert len(fallback_calls) == 1
    reason = fallback_calls[0]["degraded_reason"]
    assert reason and "RuntimeError" in reason
    assert report.summary.startswith("degraded: ")
    assert report.observations.startswith("DEGRADED MODE: ")
    for name, text in _servicenow_texts(report).items():
        assert "DEGRADED MODE: " in text, name
    caller_errors = [r.getMessage() for r in _main_logs(caplog, logging.ERROR)]
    assert any(GROUP in m and "RuntimeError" in m for m in caller_errors), caller_errors
    assert all("secret-token-xyz" not in r.getMessage() for r in _main_logs(caplog))
    assert "secret-token-xyz" not in report.summary + report.observations


@pytest.mark.parametrize("deadline_type", ["settle", "max_wait"])
def test_timer_wake_fallback_ticket_is_marked_degraded_when_investigation_fails(
    tmp_path, gcs, cloud_cfg, monkeypatch, caplog, deadline_type
):
    _seed_for_wake(gcs)
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _failing_investigation)
    fallback_calls = _spy_fallback(monkeypatch)
    tickets = _capture_tickets(monkeypatch)

    with cloud_env(tmp_path, gcs):
        res = _post_wake(cloud_cfg, deadline_type)

    assert res.status_code == 200
    assert res.json()["status"] == "executed"
    assert len(tickets) == 1
    _assert_fallback_marked_degraded(tickets[0].report, fallback_calls, caplog)
    assert _final(gcs).servicenow_sys_id == TICKET_SYS_ID


def test_investigate_fallback_report_is_marked_degraded_when_investigation_fails(
    tmp_path, gcs, cloud_cfg, monkeypatch, caplog
):
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _failing_investigation)
    fallback_calls = _spy_fallback(monkeypatch)
    callbacks: list = []
    monkeypatch.setattr(agent_main, "send_findings_to_bridge",
                        lambda report, bridge_url, config=None: callbacks.append(report) or {"status": "ok"})
    app.dependency_overrides[get_agent_settings] = lambda: cloud_cfg

    with cloud_env(tmp_path, gcs):
        res = TestClient(app).post(
            "/investigate",
            json={"alert": _alert().model_dump(mode="json"), "group_id": GROUP, "filename": FILENAME},
        )

    assert res.status_code == 200
    assert len(callbacks) == 1
    _assert_fallback_marked_degraded(callbacks[0], fallback_calls, caplog)
