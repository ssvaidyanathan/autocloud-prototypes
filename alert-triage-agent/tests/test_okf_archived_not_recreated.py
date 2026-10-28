"""A timer-wake update must not recreate an incident file that was archived meanwhile.

Review R1 (batch 2-4): the agent claims a group (status recovered) BEFORE it posts the
recovery note, then records recovery_note_posted_at with OKFStore.update_atomically. If
reset-scenarios.sh archived the file in between (gcloud storage mv), the read-modify-write
saw NotFound, started from OKFDocument(group_id=...) (status open) and uploaded a stub
`status: open` file back into knowledge/incident-manager/. The reset now waits for
recovery_note_posted_at; this is the agent-side hardening: updates of an existing group pass
create_if_missing=False, so a missing file is skipped with a WARNING naming the group.
"""

import logging
from pathlib import Path
from typing import Optional

from google.api_core import exceptions
import pytest
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import ActionStatusResponse, TicketResponse
from agent.app.contracts.report import InvestigationReport
from agent.app.contracts.timers import TimerWakeRequest
from agent.app import recovery
from agent.app.grouping.models import IncidentGroupStatus, OKFDocument
from agent.app.grouping.store import OKFStore, get_canonical_file_path
from agent.app.main import app, set_clock_func
from agent.app.timers.scheduler import set_timer_scheduler_hook

GROUP = "grp-123"
BLOB = f"knowledge/incident-manager/incident-{GROUP}.md"
SYS_ID = "sys_archived_1"


# --- fake GCS (generation preconditions, delete = archive mv) ------------------------


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self.bucket, self.name = bucket, name
        self.generation: Optional[int] = None

    def reload(self) -> None:
        if self.name not in self.bucket.objects:
            raise exceptions.NotFound(f"{self.name} not found")
        self.generation = self.bucket.objects[self.name][1]

    def download_as_text(self) -> str:
        if self.name not in self.bucket.objects:
            raise exceptions.NotFound(f"{self.name} not found")
        return self.bucket.objects[self.name][0]

    def upload_from_string(self, data, content_type="text/markdown", if_generation_match=None):
        current = self.bucket.objects.get(self.name, (None, 0))[1]
        if if_generation_match is not None and if_generation_match != current:
            raise exceptions.PreconditionFailed("generation mismatch")
        self.bucket.objects[self.name] = (data, current + 1)
        self.bucket.uploads.append((self.name, data))


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict = {}
        self.uploads: list = []

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def archive(self, name: str) -> None:
        """What `gcloud storage mv knowledge/incident-manager/*.md archive/` does to the blob."""
        self.objects["archive/" + name.rsplit("/", 1)[-1]] = self.objects.pop(name)


class FakeStorageClient:
    def __init__(self) -> None:
        self.bucket_inst = FakeBucket()

    def bucket(self, name: str) -> FakeBucket:
        return self.bucket_inst


@pytest.fixture(autouse=True)
def no_monitoring_client(monkeypatch):
    """The recovery note's traffic check reports "no client" instead of calling Monitoring."""
    monkeypatch.setattr(recovery, "_metric_client_factory", lambda settings: None)


@pytest.fixture
def gcs_store():
    client = FakeStorageClient()
    cfg = AgentConfig(project_id="proj", name_prefix="ata", bridge_url="http://mock-bridge:8080", is_local=False)
    return OKFStore(bucket_name="proj-ata-incidents", storage_client=client, is_local=False, config=cfg), client.bucket_inst


def _cooling_doc(now: float) -> OKFDocument:
    doc = OKFDocument(group_id=GROUP, status=IncidentGroupStatus.COOLING_DOWN.value, servicenow_sys_id=SYS_ID,
                      alerts=[{"incident_id": "i-1", "state": "closed", "resource_name": "ata-orders-api"}])
    doc.cooling_deadline = "2026-10-27T10:03:14Z"
    return doc


# --- the reproduced race --------------------------------------------------------------


def test_archive_between_claim_and_note_post_does_not_recreate_an_open_stub(gcs_store, monkeypatch, caplog):
    """The reviewer's probe: the reset archives right after the agent's 'recovered' claim,
    while the note is being posted. Before the fix the post-note write uploaded
    knowledge/incident-manager/incident-grp-123.md with status open."""
    store, bucket = gcs_store
    now = 1_790_000_000.0
    store.write(GROUP, _cooling_doc(now))
    seen_at_post = {}

    def post_note(self, bridge_url, request, config=None, http_client=None):
        seen_at_post["status"] = OKFDocument.from_markdown(bucket.objects[BLOB][0]).status
        bucket.archive(BLOB)  # reset step 7 lands here
        return ActionStatusResponse(status="success", sys_id=request.sys_id, group_id=request.group_id)

    monkeypatch.setattr(BridgeClient, "append_work_note", post_note)
    uploads_before = len(bucket.uploads)
    with caplog.at_level(logging.WARNING):
        res = agent_main._mark_recovered_without_resolving(store, store.read(GROUP), now, store.config)

    assert seen_at_post["status"] == IncidentGroupStatus.RECOVERED.value  # claim came first
    assert res.status == "executed"
    assert BLOB not in bucket.objects  # nothing recreated in the active folder
    assert [name for name, _ in bucket.uploads[uploads_before:]] == [BLOB]  # only the claim
    archived = OKFDocument.from_markdown(bucket.objects[f"archive/incident-{GROUP}.md"][0])
    assert archived.status == IncidentGroupStatus.RECOVERED.value
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and GROUP in r.getMessage()]
    assert any("not recreat" in r.getMessage() for r in warnings)
    assert any("Recovery note posted" in r.getMessage() for r in warnings)


def test_note_posted_at_is_recorded_when_the_file_is_still_there(gcs_store, monkeypatch):
    store, bucket = gcs_store
    now = 1_790_000_000.0
    store.write(GROUP, _cooling_doc(now))
    monkeypatch.setattr(BridgeClient, "append_work_note",
                        lambda self, bridge_url, request, config=None, http_client=None:
                        ActionStatusResponse(status="success", sys_id=request.sys_id, group_id=request.group_id))

    agent_main._mark_recovered_without_resolving(store, store.read(GROUP), now, store.config)

    doc = OKFDocument.from_markdown(bucket.objects[BLOB][0])
    assert doc.status == IncidentGroupStatus.RECOVERED.value
    assert doc.recovery_note_posted_at is not None


def test_cooling_wake_on_an_archived_file_does_not_create_one(gcs_store, monkeypatch, caplog):
    """The claim itself (read-modify-write) must not create a file either."""
    store, bucket = gcs_store
    now = 1_790_000_000.0
    stale = _cooling_doc(now)  # read by the wake before the archive
    notes = []
    monkeypatch.setattr(BridgeClient, "append_work_note", lambda *a, **k: notes.append(a))

    with caplog.at_level(logging.WARNING):
        res = agent_main._mark_recovered_without_resolving(store, stale, now, store.config)

    assert (res.status, res.reason) == ("ignored", "group_not_found")
    assert bucket.objects == {} and bucket.uploads == []
    assert notes == []
    assert any(GROUP in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


# --- OKFStore.update_atomically(create_if_missing=False) ------------------------------


def test_gcs_update_without_create_skips_a_missing_blob_with_a_warning(gcs_store, caplog):
    store, bucket = gcs_store
    with caplog.at_level(logging.WARNING, logger="agent.app.grouping.store"):
        assert store.update_atomically(GROUP, lambda d: d, create_if_missing=False) is None
    assert bucket.uploads == []
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert GROUP in record.getMessage()
    assert f"gs://proj-ata-incidents/{BLOB}" in record.getMessage()


def test_gcs_update_still_creates_by_default(gcs_store):
    store, bucket = gcs_store
    doc = store.update_atomically(GROUP, lambda d: d)
    assert doc.status == IncidentGroupStatus.OPEN.value
    assert BLOB in bucket.objects


def test_local_update_without_create_skips_a_missing_file(tmp_path, caplog):
    store = OKFStore(storage_path=tmp_path, is_local=True,
                     config=AgentConfig(project_id="proj", incident_store_path=str(tmp_path), is_local=True))
    with caplog.at_level(logging.WARNING, logger="agent.app.grouping.store"):
        assert store.update_atomically(GROUP, lambda d: d, create_if_missing=False) is None
    assert not get_canonical_file_path(tmp_path, GROUP).exists()
    assert any(GROUP in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    store.write(GROUP, OKFDocument(group_id=GROUP, status="cooling_down"))
    updated = store.update_atomically(GROUP, lambda d: setattr(d, "priority", "P2") or d, create_if_missing=False)
    assert updated.priority == "P2"


# --- settle wake: file archived while the investigation runs ---------------------------


@pytest.fixture
def local_app(tmp_path: Path, monkeypatch):
    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True)
    cfg = AgentConfig(project_id="proj", name_prefix="ata", incident_store_path=str(store_dir),
                      fallback_store_path=str(store_dir), bridge_url="http://mock-bridge:8080",
                      agent_url="http://mock-agent:8000", timer_queue_name="ata-timer-queue", is_local=True)
    state = {"now": 1000.0}
    set_clock_func(lambda: state["now"])
    set_timer_scheduler_hook(lambda **kwargs: "mock-task")
    app.dependency_overrides[get_agent_settings] = lambda: cfg
    yield TestClient(app, raise_server_exceptions=False), OKFStore(config=cfg), state, store_dir
    set_clock_func(None)
    set_timer_scheduler_hook(None)
    app.dependency_overrides.pop(get_agent_settings, None)


def test_settle_wake_does_not_recreate_a_file_archived_during_the_investigation(local_app, monkeypatch, caplog):
    client, store, clock, store_dir = local_app
    alert = AlertEvent(incident_id="0.arch-1", policy_name="projects/proj/alertPolicies/ata-orders-5xx",
                       resource_name="ata-orders-api", resource_type="cloud_run_revision", state="open",
                       started_at="2026-10-24T00:16:40Z").model_dump(mode="json")
    group_id = client.post("/triage", json=alert).json()["group_id"]
    path = get_canonical_file_path(store_dir, group_id)

    async def investigate_then_archive(primary_alert, gid, filename, settings, group_alerts=None):
        path.unlink()  # reset archived the file meanwhile
        return InvestigationReport(group_id=gid, summary="stub", observations="stub")

    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", investigate_then_archive)
    monkeypatch.setattr(BridgeClient, "create_ticket", lambda self, bridge_url, request, config=None, http_client=None:
                        TicketResponse(status="created", sys_id=SYS_ID, number="INC1", url="https://x",
                                       group_id=request.group_id))
    clock["now"] = store.read(group_id).settle_deadline_epoch()
    req = TimerWakeRequest(group_id=group_id, deadline_type="settle", scheduled_epoch=int(clock["now"]))
    with caplog.at_level(logging.WARNING):
        res = client.post("/timer/wake", json=req.model_dump(mode="json"))

    assert res.status_code == 200, res.text
    assert res.json()["reason"] == "group_file_gone"
    assert not path.exists()
    assert any(group_id in r.getMessage() and "INC1" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)

