"""scripts/prune-revisions.sh: list (dry run) and, with --apply, delete old 0%-traffic
revisions of the demo services. Never the pinned revision, the previous pin, --keep,
serving or tagged revisions, or the latest created/ready revision."""

import pytest

from tests.cloud_run_fake import (
    BASELINE_ENV, IMAGE_BASE, INV, NEW_DIGEST, OLD_DIGEST, OLD_PIN, ORD, REPO_ROOT, FakeCloud,
    live_like_state, write_pin,
)

PRUNE = REPO_ROOT / "scripts" / "prune-revisions.sh"
NEW_PIN = "ata-inventory-api-pin-20261028000000"


def rev(service, created, digest=OLD_DIGEST, **kw):
    r = {"service": service, "image": f"{IMAGE_BASE}@{digest}", "digest": digest, "env": dict(BASELINE_ENV),
         "max": "1", "min": "0", "ready": True, "created": created}
    r.update(kw)
    return r


@pytest.fixture
def cloud(tmp_path):
    state = live_like_state()
    revs = state["revisions"]
    revs["ata-inventory-api-00100-old"] = rev(INV, "2026-10-20T00:00:00Z")
    revs["ata-inventory-api-s5-1793109672"] = rev(INV, "2026-10-27T09:00:00Z")
    revs["ata-inventory-api-00146-p5d"] = rev(INV, "2026-10-27T14:00:00Z")  # latest created/ready
    revs["ata-orders-api-00001-aaa"] = rev(ORD, "2026-10-01T00:00:00Z")
    return FakeCloud(tmp_path, state)


def test_dry_run_lists_candidates_and_deletes_nothing(cloud):
    res = cloud.run([PRUNE])

    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "DELETE ata-inventory-api-00100-old" in out
    assert "DELETE ata-inventory-api-s5-1793109672" in out
    assert "DELETE ata-orders-api-00001-aaa" in out
    assert f"KEEP  {OLD_PIN}" in out and "serves 100%" in out
    assert "KEEP  ata-inventory-api-00146-p5d" in out and "latest created" in out
    assert "KEEP  ata-orders-api-00004-rgg" in out
    assert out.index("DELETE ata-inventory-api-00100-old") < out.index("DELETE ata-inventory-api-s5-1793109672")
    assert "3 revision(s) would be deleted" in res.stderr
    assert cloud.calls(["run", "revisions", "delete"]) == []


def test_apply_deletes_only_the_candidates(cloud):
    res = cloud.run([PRUNE, "--apply"])

    assert res.returncode == 0, res.stdout + res.stderr
    deleted = {c[3] for c in cloud.calls(["run", "revisions", "delete"])}
    assert deleted == {"ata-inventory-api-00100-old", "ata-inventory-api-s5-1793109672", "ata-orders-api-00001-aaa"}
    left = set(cloud.state["revisions"])
    assert {OLD_PIN, "ata-inventory-api-00146-p5d", "ata-orders-api-00004-rgg"} <= left
    assert cloud.serving() == [(OLD_PIN, 100)]


def test_pinned_and_previous_pin_are_never_deleted_even_with_no_traffic(cloud):
    """After a re-pin the old pin has 0% traffic: it is the rollback target and stays."""
    state = cloud.state
    state["revisions"][NEW_PIN] = rev(INV, "2026-10-28T00:00:00Z", digest=NEW_DIGEST)
    state["services"][INV]["traffic"] = [{"revisionName": NEW_PIN, "percent": 100}]
    cloud.set_state(state)
    write_pin(cloud.pin_file, NEW_PIN, NEW_DIGEST, OLD_PIN, OLD_DIGEST)

    res = cloud.run([PRUNE, "--apply"])

    assert res.returncode == 0, res.stdout + res.stderr
    deleted = {c[3] for c in cloud.calls(["run", "revisions", "delete"])}
    assert OLD_PIN not in deleted and NEW_PIN not in deleted
    assert f"KEEP  {OLD_PIN}" in res.stdout and "protected" in res.stdout


def test_pin_is_protected_even_when_traffic_is_elsewhere_in_dry_run(cloud):
    state = cloud.state
    state["services"][INV]["traffic"] = [{"revisionName": "ata-inventory-api-s5-1793109672", "percent": 100}]
    cloud.set_state(state)
    res = cloud.run([PRUNE])
    assert res.returncode == 0
    assert f"DELETE {OLD_PIN}" not in res.stdout and f"KEEP  {OLD_PIN}" in res.stdout


def test_apply_refuses_while_inventory_is_off_the_pin(cloud):
    state = cloud.state
    state["services"][INV]["traffic"] = [{"revisionName": "ata-inventory-api-s5-1793109672", "percent": 100}]
    cloud.set_state(state)
    res = cloud.run([PRUNE, "--apply"])
    assert res.returncode != 0
    assert "not pruning while" in res.stderr
    assert cloud.calls(["run", "revisions", "delete"]) == []


def test_keep_and_service_filter(cloud):
    res = cloud.run([PRUNE, "--apply", "--keep", "ata-inventory-api-00100-old", "--service", "inventory"])
    assert res.returncode == 0, res.stdout + res.stderr
    deleted = {c[3] for c in cloud.calls(["run", "revisions", "delete"])}
    assert deleted == {"ata-inventory-api-s5-1793109672"}


def test_tagged_revision_is_kept(cloud):
    state = cloud.state
    state["services"][INV]["traffic"].append(
        {"revisionName": "ata-inventory-api-00100-old", "tag": "repin-candidate", "url": "https://t"})
    cloud.set_state(state)
    res = cloud.run([PRUNE])
    assert "KEEP  ata-inventory-api-00100-old" in res.stdout and "traffic tag repin-candidate" in res.stdout


def test_a_failed_delete_is_reported_and_fails_the_run(cloud):
    state = cloud.state
    state["fail"] = ["delete:ata-inventory-api-00100-old"]
    cloud.set_state(state)
    res = cloud.run([PRUNE, "--apply"])
    assert res.returncode != 0
    assert "FAILED to delete ata-inventory-api-00100-old" in res.stderr
    assert "ata-inventory-api-s5-1793109672" not in cloud.state["revisions"]  # the others still go


@pytest.mark.parametrize("args, text", [(["--service", "agent"], "expected inventory or orders"),
                                        (["--keep", "Bad Name"], "is not a revision name"),
                                        (["--bogus"], "Unknown option")])
def test_bad_arguments(cloud, args, text):
    res = cloud.run([PRUNE, *args])
    assert res.returncode != 0 and text in res.stderr
    assert cloud.calls(["run", "revisions", "delete"]) == []


def test_list_failure_is_fatal(cloud):
    state = cloud.state
    state["fail"] = ["list"]
    cloud.set_state(state)
    res = cloud.run([PRUNE])
    assert res.returncode != 0 and "cannot list revisions" in res.stderr
