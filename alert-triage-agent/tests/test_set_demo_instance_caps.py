"""scripts/set-demo-instance-caps.sh: service-level max instances on both demo services
(covers every revision, including the pre-cap pinned inventory revision, with no new
revision) and the orders revision-level limits. Dry run by default."""

import pytest

from tests.cloud_run_fake import INV, ORD, OLD_PIN, REPO_ROOT, FakeCloud, live_like_state

CAPS = REPO_ROOT / "scripts" / "set-demo-instance-caps.sh"


@pytest.fixture
def cloud(tmp_path):
    return FakeCloud(tmp_path, live_like_state())


def test_dry_run_is_read_only_and_lists_the_commands(cloud):
    res = cloud.run([CAPS])

    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.calls(["run", "services", "update"]) == []
    out = res.stdout + res.stderr
    assert f"WOULD RUN ({INV} service-level max): gcloud run services update {INV}" in out
    assert "--max=1" in out and "--max-instances=1 --min-instances=0" in out
    assert "3 change(s) pending" in out


def test_apply_sets_service_level_max_without_a_new_inventory_revision(cloud):
    before = set(cloud.state["revisions"])

    res = cloud.run([CAPS, "--apply"])

    assert res.returncode == 0, res.stdout + res.stderr
    state = cloud.state
    assert state["services"][INV]["service_max"] == "1"
    assert state["services"][ORD]["service_max"] == "1"
    new = set(state["revisions"]) - before
    # Only orders got a revision (revision-level limits); inventory traffic stays on its pin.
    assert len(new) == 1 and next(iter(new)).startswith(ORD)
    assert cloud.serving(INV) == [(OLD_PIN, 100)]
    assert cloud.serving(ORD) == [(next(iter(new)), 100)]
    inv_updates = [c for c in cloud.calls(["run", "services", "update"]) if c[3] == INV]
    assert inv_updates and all(set(a.split("=")[0] for a in c[4:]) <= {"--project", "--region", "--max", "--quiet"}
                               for c in inv_updates)
    assert "verified" in res.stdout


def test_apply_is_idempotent(cloud):
    assert cloud.run([CAPS, "--apply"]).returncode == 0
    n = len(cloud.calls(["run", "services", "update"]))

    res = cloud.run([CAPS, "--apply"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert len(cloud.calls(["run", "services", "update"])) == n
    assert cloud.run([CAPS]).stdout.count("already") == 3


def test_apply_fails_loudly_when_the_limit_does_not_stick(cloud):
    state = cloud.state
    state["fail"] = ["ignore-max-instances"]
    cloud.set_state(state)

    res = cloud.run([CAPS, "--apply"])

    assert res.returncode != 0
    assert "maxScale=100 (want 1)" in res.stderr
    assert "instance-limit check(s) failed" in res.stderr


def test_configured_cap_is_used_and_validated(cloud):
    res = cloud.run([CAPS, "--apply"], ATA_DEMO_MAX_INSTANCES="2")
    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.state["services"][INV]["service_max"] == "2"

    bad = cloud.run([CAPS], ATA_DEMO_MAX_INSTANCES="one")
    assert bad.returncode != 0
    assert "must be a whole number >= 1" in bad.stderr


def test_describe_failure_is_fatal(cloud):
    state = cloud.state
    state["fail"] = ["describe"]
    cloud.set_state(state)
    res = cloud.run([CAPS])
    assert res.returncode != 0
    assert "cannot describe" in res.stderr
