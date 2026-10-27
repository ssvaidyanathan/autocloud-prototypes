"""Concurrency tests for the duplicate-alert append path.

The duplicate path (``append_duplicate_alert_to_incident``) runs on the triage queue while
timer wakes (ticket minting, status transitions) write the same incident group from the
timer queue. These tests use the generation-enforcing GCS mock to verify that:

1. A timer ``_mark_ticketed`` write racing a duplicate append is not lost.
2. A 412 on the duplicate path is retried against a fresh read.
3. Re-delivery of the same duplicate alert is appended once.
4. Storage failures propagate (``/triage`` returns 5xx so Cloud Tasks retries).
5. A vanished group is not resurrected; ``/triage`` falls back to grouping.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from google.api_core import exceptions
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.duplicate_checker import (
    DuplicateCheckResult,
    DuplicateTargetMissingError,
    append_duplicate_alert_to_incident,
)
from agent.app.grouping.models import OKFDocument
from agent.app.grouping.store import ConcurrencyLockError, OKFStore
from agent.app.main import app, set_clock_func
from agent.app.timers.scheduler import set_timer_scheduler_hook
from tests.test_agent_grouping_and_timers import (
    ConcurrencyMockBlob,
    ConcurrencyMockStorageClient,
)

BUCKET = "test-incidents"
GROUP = "ata-inventory-5xx-ata-inventory-api-dup"
FILENAME = f"incident-{GROUP}.md"
BLOB = f"knowledge/incident-manager/{FILENAME}"


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
def gcs() -> ConcurrencyMockStorageClient:
    return ConcurrencyMockStorageClient()


def _seed(gcs: ConcurrencyMockStorageClient, tmp_path: Path, status: str = "investigating") -> tuple[OKFStore, Path]:
    """Write the group to the mock bucket and mirror it locally (stands in for the FUSE view)."""
    store = OKFStore(bucket_name=BUCKET, storage_client=gcs, is_local=False)
    doc = OKFDocument(
        group_id=GROUP,
        status=status,
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        timestamp="2026-10-27T03:00:00Z",
        alerts=[{
            "incident_id": "0.first",
            "policy_name": "ata-inventory-5xx",
            "resource_name": "ata-inventory-api",
            "state": "open",
            "started_at": "2026-10-27T03:00:00Z",
        }],
        body="# Incident",
    )
    store.write(GROUP, doc)
    local = tmp_path / FILENAME
    local.write_text(_blob(gcs).content, encoding="utf-8")
    return store, local


def _blob(gcs: ConcurrencyMockStorageClient) -> ConcurrencyMockBlob:
    return gcs.bucket(BUCKET).blob(BLOB)


def _final(gcs: ConcurrencyMockStorageClient) -> OKFDocument:
    return OKFDocument.from_markdown(_blob(gcs).content)


def _dup_alert(incident_id: str = "0.dup") -> AlertEvent:
    return AlertEvent(
        incident_id=incident_id,
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at="2026-10-27T03:04:00Z",
    )


def _mark_ticketed(doc: OKFDocument) -> OKFDocument:
    doc.servicenow_sys_id = "sys_123"
    doc.servicenow_number = "INC0010001"
    doc.status = "ticketed"
    return doc


def test_timer_ticket_write_racing_duplicate_append_survives(tmp_path, gcs, cloud_cfg):
    """Regression (dup-investigation PoC): timer _mark_ticketed lands between dup read and write."""
    store, local = _seed(gcs, tmp_path)
    real_upload = ConcurrencyMockBlob.upload_from_string
    raced = {"done": False}

    def racing_upload(self, data, content_type="text/markdown", if_generation_match=None):
        # The first duplicate-path write is preceded by a concurrent timer write.
        if not raced["done"]:
            raced["done"] = True
            store.update_atomically(GROUP, _mark_ticketed)
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    with patch("google.cloud.storage.Client", return_value=gcs), \
         patch.object(ConcurrencyMockBlob, "upload_from_string", racing_upload):
        meta = append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)

    final = _final(gcs)
    assert raced["done"] is True
    assert final.servicenow_sys_id == "sys_123"
    assert final.servicenow_number == "INC0010001"
    assert str(final.status) == "ticketed"
    assert [a["incident_id"] for a in final.alerts] == ["0.first", "0.dup"]
    assert meta["servicenow_sys_id"] == "sys_123"
    assert meta["status"] == "ticketed"
    # seed (1) + timer write (2) + duplicate append retried on fresh read (3)
    assert _blob(gcs).generation == 3


def test_duplicate_append_retries_after_412(tmp_path, gcs, cloud_cfg):
    """A 412 on the duplicate write re-reads and re-applies the append on the fresh document."""
    _, local = _seed(gcs, tmp_path)
    blob = _blob(gcs)
    real_upload = ConcurrencyMockBlob.upload_from_string
    attempts: list[int | None] = []

    def flaky_upload(self, data, content_type="text/markdown", if_generation_match=None):
        attempts.append(if_generation_match)
        if len(attempts) == 1:
            # A concurrent writer commits first, then this conditional write is rejected.
            real_upload(self, _mark_ticketed(OKFDocument.from_markdown(self.content)).to_markdown())
            raise exceptions.PreconditionFailed("generation mismatch")
        return real_upload(self, data, content_type=content_type, if_generation_match=if_generation_match)

    with patch("google.cloud.storage.Client", return_value=gcs), \
         patch.object(ConcurrencyMockBlob, "upload_from_string", flaky_upload), \
         patch("agent.app.grouping.store.time.sleep"):
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)

    assert attempts == [1, 2]
    final = _final(gcs)
    assert final.servicenow_sys_id == "sys_123"
    assert str(final.status) == "ticketed"
    assert [a["incident_id"] for a in final.alerts] == ["0.first", "0.dup"]
    assert blob.content.count("**Duplicate Alert Appended**") == 1


def test_duplicate_promotes_investigating_status(tmp_path, gcs, cloud_cfg):
    """Existing semantics preserved: investigating is promoted to mitigation-in-progress."""
    _, local = _seed(gcs, tmp_path, status="investigating")
    with patch("google.cloud.storage.Client", return_value=gcs):
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)
    final = _final(gcs)
    assert str(final.status) == "mitigation-in-progress"
    assert final.timestamp == "2026-10-27T03:04:00Z"


def test_duplicate_redelivery_is_idempotent_gcs(tmp_path, gcs, cloud_cfg):
    """The same duplicate delivered twice (Cloud Tasks retry) is recorded once."""
    _, local = _seed(gcs, tmp_path)
    with patch("google.cloud.storage.Client", return_value=gcs):
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)

    final = _final(gcs)
    assert [a["incident_id"] for a in final.alerts] == ["0.first", "0.dup"]
    assert _blob(gcs).content.count("**Duplicate Alert Appended**") == 1


def test_duplicate_redelivery_is_idempotent_local(tmp_path):
    """Local (file-locked) backend: same duplicate twice is appended once."""
    store = OKFStore(storage_path=tmp_path, is_local=True)
    store.write(GROUP, OKFDocument(
        group_id=GROUP,
        status="ticketed",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        servicenow_sys_id="sys_local",
        alerts=[{"incident_id": "0.first", "started_at": "2026-10-27T03:00:00Z"}],
    ))
    local = tmp_path / FILENAME

    append_duplicate_alert_to_incident(local, _dup_alert())
    append_duplicate_alert_to_incident(local, _dup_alert())

    content = local.read_text(encoding="utf-8")
    final = OKFDocument.from_markdown(content)
    assert [a["incident_id"] for a in final.alerts] == ["0.first", "0.dup"]
    assert content.count("**Duplicate Alert Appended**") == 1
    assert final.servicenow_sys_id == "sys_local"
    assert str(final.status) == "ticketed"


def test_duplicate_append_raises_when_retries_exhausted(tmp_path, gcs, cloud_cfg):
    """Persistent 412s raise ConcurrencyLockError instead of silently dropping the update."""
    _, local = _seed(gcs, tmp_path)
    before = _blob(gcs).content

    def always_412(self, data, content_type="text/markdown", if_generation_match=None):
        raise exceptions.PreconditionFailed("generation mismatch")

    with patch("google.cloud.storage.Client", return_value=gcs), \
         patch.object(ConcurrencyMockBlob, "upload_from_string", always_412), \
         patch("agent.app.grouping.store.time.sleep"), \
         pytest.raises(ConcurrencyLockError):
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)

    assert _blob(gcs).content == before


def test_duplicate_append_propagates_storage_errors(tmp_path, gcs, cloud_cfg):
    """Non-412 GCS errors propagate (previously swallowed at debug level)."""
    _, local = _seed(gcs, tmp_path)

    def unavailable(self, data, content_type="text/markdown", if_generation_match=None):
        raise exceptions.ServiceUnavailable("gcs down")

    with patch("google.cloud.storage.Client", return_value=gcs), \
         patch.object(ConcurrencyMockBlob, "upload_from_string", unavailable), \
         pytest.raises(exceptions.ServiceUnavailable):
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)


def test_duplicate_append_does_not_write_through_fuse(tmp_path, gcs, cloud_cfg):
    """In cloud mode the FUSE file is not written; only the conditional GCS update is."""
    _, local = _seed(gcs, tmp_path)
    before = local.read_text(encoding="utf-8")
    with patch("google.cloud.storage.Client", return_value=gcs):
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)
    assert local.read_text(encoding="utf-8") == before
    assert "0.dup" in _blob(gcs).content


def test_duplicate_append_does_not_resurrect_missing_group(tmp_path, gcs, cloud_cfg):
    """A group deleted after the grep match raises and is not recreated as an empty doc."""
    local = tmp_path / FILENAME
    with patch("google.cloud.storage.Client", return_value=gcs), \
         pytest.raises(DuplicateTargetMissingError):
        append_duplicate_alert_to_incident(local, _dup_alert(), config=cloud_cfg)
    assert _blob(gcs).content is None


def _triage_client(cfg: AgentConfig, active_file: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        agent_main,
        "check_for_duplicate",
        lambda **_: DuplicateCheckResult(
            is_duplicate=True,
            status="ACTIVE_DUPLICATE_PROCESSING",
            matching_files=[str(active_file)],
            active_file=str(active_file),
        ),
    )
    app.dependency_overrides[get_agent_settings] = lambda: cfg
    return TestClient(app, raise_server_exceptions=False)


def test_triage_returns_5xx_when_duplicate_append_fails(tmp_path, gcs, cloud_cfg, monkeypatch):
    """/triage surfaces duplicate-path storage failures so Cloud Tasks redelivers."""
    _, local = _seed(gcs, tmp_path)
    client = _triage_client(cloud_cfg, local, monkeypatch)

    def always_412(self, data, content_type="text/markdown", if_generation_match=None):
        raise exceptions.PreconditionFailed("generation mismatch")

    with patch("google.cloud.storage.Client", return_value=gcs), \
         patch.object(ConcurrencyMockBlob, "upload_from_string", always_412), \
         patch("agent.app.grouping.store.time.sleep"):
        res = client.post("/triage", json=_dup_alert().model_dump(mode="json"))

    assert res.status_code >= 500
    assert "duplicate" not in res.text


def test_triage_duplicate_success_contract(tmp_path, gcs, cloud_cfg, monkeypatch):
    """/triage keeps its duplicate response contract on success."""
    _, local = _seed(gcs, tmp_path)
    client = _triage_client(cloud_cfg, local, monkeypatch)
    with patch("google.cloud.storage.Client", return_value=gcs):
        res = client.post("/triage", json=_dup_alert().model_dump(mode="json"))

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "duplicate"
    assert body["group_id"] == GROUP
    assert body["filename"] == FILENAME
    assert "0.dup" in _blob(gcs).content


def test_triage_falls_back_to_grouping_when_duplicate_target_vanished(tmp_path, gcs, cloud_cfg, monkeypatch):
    """A vanished duplicate target routes the alert through normal grouping (no silent drop)."""
    local = tmp_path / FILENAME
    client = _triage_client(cloud_cfg, local, monkeypatch)
    with patch("google.cloud.storage.Client", return_value=gcs):
        res = client.post("/triage", json=_dup_alert().model_dump(mode="json"))

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "triaged"
    assert _blob(gcs).content is None
    store = OKFStore(bucket_name=BUCKET, storage_client=gcs, is_local=False)
    new_group = store.read(body["group_id"])
    assert new_group is not None
    assert new_group.has_alert("0.dup")
