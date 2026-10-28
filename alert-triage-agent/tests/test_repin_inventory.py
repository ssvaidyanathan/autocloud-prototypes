"""scripts/repin-inventory.sh: move the inventory baseline to a new revision safely.

The live pin (ata-inventory-api-00073-9vb) predates the one-instance cap (its maxScale is
100; revision scaling is immutable) and carries the DB_USER/MOCK_DB aliases. A new pin
must be deployed with NO traffic, verified, written to inventory-pin.<prefix>.env (the one place
the pin lives), and only then receive 100% of traffic. Rollback swaps back.

Runs under ATA_TEST_BASH (bash 3.2 for macOS parity) with a stateful fake gcloud/curl.
"""

import re

import pytest

from tests.cloud_run_fake import (
    BASELINE_ENV, IMAGE_BASE, INV, INV_URL, NEW_DIGEST, OLD_DIGEST, OLD_PIN, REPO_ROOT,
    FakeCloud, for_prefix, fresh_project_state, healthy_http, live_like_state, without, write_pin,
)

REPIN = REPO_ROOT / "scripts" / "repin-inventory.sh"
CAND_URL = "https://repin-candidate---inv.example"


@pytest.fixture
def cloud(tmp_path):
    return FakeCloud(tmp_path, live_like_state())


def candidate_name(cloud):
    update = cloud.calls(["run", "services", "update"])[0]
    suffix = next(a.split("=", 1)[1] for a in update if a.startswith("--revision-suffix="))
    return f"{INV}-{suffix}"


def prepared(cloud):
    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])
    assert res.returncode == 0, res.stdout + res.stderr
    return candidate_name(cloud)


# --- prepare --------------------------------------------------------------------------


def test_prepare_deploys_a_no_traffic_candidate_verifies_it_and_removes_the_tag(cloud):
    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])

    assert res.returncode == 0, res.stdout + res.stderr
    cand = candidate_name(cloud)
    assert re.fullmatch(r"ata-inventory-api-pin-\d{14}", cand)
    update = cloud.calls(["run", "services", "update"])[0]
    for flag in ("--no-traffic", "--tag=repin-candidate", f"--image={IMAGE_BASE}@{NEW_DIGEST}",
                 "--remove-env-vars=DB_USER,MOCK_DB", "--max-instances=1", "--min-instances=0"):
        assert flag in update, flag
    # Serving never moved; the tag is gone (it would break the exact `rev,100` checks).
    assert cloud.serving() == [(OLD_PIN, 100)]
    assert cloud.tags() == []
    rev = cloud.state["revisions"][cand]
    assert rev["env"] == BASELINE_ENV and (rev["max"], rev["min"]) == ("1", "0")
    # HTTP checks went to the candidate's tag URL, GET only (no restock or other write).
    urls = [a for c in cloud.curl_calls() for a in c if a.startswith("http")]
    assert urls and all(u.startswith(CAND_URL + "/") for u in urls)
    assert f"{CAND_URL}/items" in urls and f"{CAND_URL}/admin/restock" in urls
    assert cloud.curl_bodies() == [] and not any("-X" in c for c in cloud.curl_calls())
    assert f"write-pin --revision {cand} --digest {NEW_DIGEST}" in res.stdout
    assert cloud.calls(["builds", "submit"]) == []


def test_prepare_with_image_tag_builds_first(cloud):
    res = cloud.run([REPIN, "prepare", "--image-tag", "v1.2.0"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.state["built_tag"] == f"{IMAGE_BASE}:v1.2.0"
    assert cloud.state["revisions"][candidate_name(cloud)]["digest"] == NEW_DIGEST


def test_prepare_build_reads_the_digest_from_the_registry_not_the_streamed_build_log(cloud):
    """Live ata rollout 2026-10-27: gcloud builds submit streams the build log on stdout, so
    capturing it as "the digest" failed ("Cloud Build returned no image digest") after a
    successful push. The log must go to stderr and the digest come from Artifact Registry."""
    res = cloud.run([REPIN, "prepare", "--image-tag", "v1.2.0"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert "REMOTE BUILD OUTPUT" in res.stderr and "REMOTE BUILD OUTPUT" not in res.stdout
    (describe,) = cloud.calls(["artifacts", "docker", "images", "describe"])
    assert describe[4] == f"{IMAGE_BASE}:v1.2.0"
    assert "--format=value(image_summary.digest)" in describe
    assert f"write-pin --revision {candidate_name(cloud)} --digest {NEW_DIGEST}" in res.stdout


def test_prepare_fails_loudly_when_the_pushed_digest_cannot_be_read(cloud):
    state = cloud.state
    state["fail"] = ["ar_describe"]
    cloud.set_state(state)

    res = cloud.run([REPIN, "prepare", "--image-tag", "v1.2.0"])

    assert res.returncode != 0
    assert "its digest could not be read from Artifact Registry" in res.stderr
    assert "nothing was deployed" in res.stderr
    assert cloud.calls(["run", "services", "update"]) == []
    assert cloud.serving() == [(OLD_PIN, 100)]


@pytest.mark.parametrize(
    "args, message",
    [
        ([], "--image-tag <tag> (build) or --image-digest sha256:<hex> is required"),
        (["--image-digest", "sha256:abc"], "is not sha256:<64 hex>"),
        (["--image-tag", "v1", "--image-digest", NEW_DIGEST], "not both"),
        (["--image-tag", "bad tag"], "is not a valid Docker tag"),
    ],
)
def test_prepare_argument_errors_change_nothing(cloud, args, message):
    res = cloud.run([REPIN, "prepare", *args])
    assert res.returncode != 0
    assert message in res.stderr
    assert cloud.calls(["run", "services", "update"]) == []


def test_prepare_refuses_when_traffic_is_not_on_the_pin(cloud):
    state = cloud.state
    state["services"][INV]["traffic"] = [{"revisionName": "ata-inventory-api-s5-1", "percent": 100}]
    cloud.set_state(state)

    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])

    assert res.returncode != 0
    assert "run ./scripts/reset-scenarios.sh first" in res.stderr
    assert cloud.calls(["run", "services", "update"]) == []


def test_prepare_fails_loudly_and_untags_when_the_candidate_misses_the_cap(cloud):
    state = cloud.state
    state["fail"] = ["ignore-max-instances"]
    cloud.set_state(state)

    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])

    assert res.returncode != 0
    assert "maxScale=100 (want 1)" in res.stderr
    assert "is not a valid baseline revision" in res.stderr
    assert cloud.serving() == [(OLD_PIN, 100)] and cloud.tags() == []


def test_prepare_fails_loudly_and_untags_when_the_database_path_fails(cloud):
    state = cloud.state
    state["http"][CAND_URL]["GET /items"] = [500, {"detail": "Database query error: password authentication failed"}]
    cloud.set_state(state)

    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])

    assert res.returncode != 0
    assert "GET https://repin-candidate---inv.example/items -> HTTP 500" in res.stderr
    assert "failed the HTTP checks" in res.stderr
    assert cloud.serving() == [(OLD_PIN, 100)] and cloud.tags() == []


def test_prepare_requires_the_restock_endpoint_on_the_candidate(cloud):
    state = cloud.state
    del state["http"][CAND_URL]["GET /admin/restock"]
    cloud.set_state(state)

    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])

    assert res.returncode != 0
    assert "/admin/restock -> HTTP 404" in res.stderr


def test_prepare_says_what_is_left_when_the_tag_cannot_be_removed(cloud):
    state = cloud.state
    state["fail"] = ["remove-tags"]
    cloud.set_state(state)

    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])

    assert res.returncode != 0
    assert "STILL LEFT: traffic tag repin-candidate" in res.stderr


def test_prepare_refuses_a_leftover_candidate_tag(cloud):
    state = cloud.state
    state["services"][INV]["traffic"].append({"revisionName": "x", "tag": "repin-candidate", "url": CAND_URL})
    cloud.set_state(state)

    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])

    assert res.returncode != 0
    assert "already exists" in res.stderr
    assert cloud.calls(["run", "services", "update"]) == []


# --- write-pin ------------------------------------------------------------------------


def test_write_pin_rewrites_the_pin_file_and_keeps_the_previous_pin(cloud):
    cand = prepared(cloud)

    res = cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST])

    assert res.returncode == 0, res.stdout + res.stderr
    text = cloud.pin_file.read_text()
    assert f"PIN_INVENTORY_REVISION={cand}\n" in text
    assert f"PIN_INVENTORY_DIGEST={NEW_DIGEST}\n" in text
    assert f"PIN_INVENTORY_PREVIOUS_REVISION={OLD_PIN}\n" in text
    assert f"PIN_INVENTORY_PREVIOUS_DIGEST={OLD_DIGEST}\n" in text
    assert text.startswith("# test pin\n")  # header comments kept
    assert cloud.serving() == [(OLD_PIN, 100)]  # write-pin never moves traffic
    # Only prepare's tag removal touched traffic.
    assert not any(a.startswith("--to-revisions") for c in cloud.calls(["run", "services", "update-traffic"]) for a in c)


def test_write_pin_refuses_a_revision_off_baseline(cloud):
    """The legacy-shaped latest revision (maxScale 100, aliases) must not become the pin."""
    state = cloud.state
    state["revisions"]["ata-inventory-api-00146-p5d"] = dict(state["revisions"][OLD_PIN], digest=NEW_DIGEST,
                                                             image=f"{IMAGE_BASE}@{NEW_DIGEST}")
    cloud.set_state(state)
    before = cloud.pin_file.read_text()

    res = cloud.run([REPIN, "write-pin", "--revision", "ata-inventory-api-00146-p5d", "--digest", NEW_DIGEST])

    assert res.returncode != 0
    assert "DB_USER is set (legacy alias; must be absent)" in res.stderr
    assert "maxScale=100 (want 1)" in res.stderr
    assert cloud.pin_file.read_text() == before


def test_write_pin_refuses_a_digest_mismatch(cloud):
    cand = prepared(cloud)
    before = cloud.pin_file.read_text()
    res = cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", "sha256:" + "ef" * 32])
    assert res.returncode != 0
    assert "imageDigest=" in res.stderr
    assert cloud.pin_file.read_text() == before


def test_pin_overrides_in_the_environment_are_refused(cloud):
    res = cloud.run([REPIN, "switch"], PINNED_INVENTORY_REVISION="ata-inventory-api-00001-abc")
    assert res.returncode != 0
    assert "overridden in the environment" in res.stderr


# --- switch / rollback ----------------------------------------------------------------


def test_switch_moves_all_traffic_to_the_new_pin_and_verifies(cloud):
    cand = prepared(cloud)
    assert cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST]).returncode == 0

    res = cloud.run([REPIN, "switch"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.serving() == [(cand, 100)]
    assert f"--to-revisions={cand}=100" in cloud.calls(["run", "services", "update-traffic"])[-1]
    assert f"{INV_URL}/items" in [a for c in cloud.curl_calls() for a in c]
    assert "reset-scenarios.sh (restocks the demo items)" in res.stdout


def test_switch_refuses_the_legacy_pin(cloud):
    """00073-9vb has maxScale 100 and the aliases: switch must not treat it as a baseline."""
    res = cloud.run([REPIN, "switch"])

    assert res.returncode != 0
    assert "is not a valid baseline revision" in res.stderr
    assert cloud.calls(["run", "services", "update-traffic"]) == []


def test_switch_failure_after_routing_names_the_rollback_and_does_not_roll_back(cloud):
    cand = prepared(cloud)
    assert cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST]).returncode == 0
    state = cloud.state
    state["http"][INV_URL]["GET /items"] = [500, {"detail": "boom"}]
    cloud.set_state(state)

    res = cloud.run([REPIN, "switch"])

    assert res.returncode != 0
    assert "Roll back:" in res.stderr and f"traffic to {OLD_PIN}" in res.stderr
    assert cloud.serving() == [(cand, 100)]  # no automatic rollback


def test_rollback_restores_the_previous_pin_and_swaps_the_file(cloud):
    cand = prepared(cloud)
    assert cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST]).returncode == 0
    assert cloud.run([REPIN, "switch"]).returncode == 0

    res = cloud.run([REPIN, "rollback"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.serving() == [(OLD_PIN, 100)]
    text = cloud.pin_file.read_text()
    assert f"PIN_INVENTORY_REVISION={OLD_PIN}\n" in text
    assert f"PIN_INVENTORY_PREVIOUS_REVISION={cand}\n" in text
    # The legacy revision's differences are reported, not hidden.
    assert "differs from the current baseline" in res.stderr


def _switched_with_http_by_revision(cloud, old_answers):
    """prepare -> write-pin -> switch; afterwards HTTP answers follow the serving revision:
    the new pin has current code, OLD_PIN answers old_answers."""
    cand = prepared(cloud)
    assert cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST]).returncode == 0
    assert cloud.run([REPIN, "switch"]).returncode == 0
    state = cloud.state
    state["http_rev"] = {cand: healthy_http(INV_URL), OLD_PIN: old_answers}
    cloud.set_state(state)
    return cand


def test_rollback_to_a_pin_without_restock_swaps_the_pin_and_says_no_restock(cloud):
    """Review C1: the legacy pin (00073-9vb) predates /admin/restock (404). Rollback must
    succeed with a warning, the pin file must name the serving revision, and the next step
    is reset --no-restock (a plain reset would fail at the restock step)."""
    cand = _switched_with_http_by_revision(cloud, without(healthy_http(INV_URL), "GET /admin/restock"))

    res = cloud.run([REPIN, "rollback"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.serving() == [(OLD_PIN, 100)]
    text = cloud.pin_file.read_text()
    assert f"PIN_INVENTORY_REVISION={OLD_PIN}\n" in text
    assert f"PIN_INVENTORY_PREVIOUS_REVISION={cand}\n" in text
    assert "predates /admin/restock" in res.stderr
    assert "./scripts/reset-scenarios.sh --no-restock" in res.stdout
    # The probe was really sent to the old code (the fake answered 404 for it).
    assert any(c[-1] == f"{INV_URL}/admin/restock" for c in cloud.curl_calls())


def test_rollback_with_restock_present_keeps_the_plain_reset(cloud):
    _switched_with_http_by_revision(cloud, healthy_http(INV_URL))
    res = cloud.run([REPIN, "rollback"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "--no-restock" not in res.stdout and "predates /admin/restock" not in res.stderr
    assert "./scripts/reset-scenarios.sh\n" in res.stdout


def test_rollback_http_failure_after_routing_still_swaps_the_pin(cloud):
    """Traffic moved: the pin file must follow it even when the old revision fails checks,
    or the next reset routes traffic back to the pin being rolled back from."""
    cand = _switched_with_http_by_revision(cloud, without(healthy_http(INV_URL), "GET /items"))

    res = cloud.run([REPIN, "rollback"])

    assert res.returncode != 0
    assert cloud.serving() == [(OLD_PIN, 100)]
    text = cloud.pin_file.read_text()
    assert f"PIN_INVENTORY_REVISION={OLD_PIN}\n" in text
    assert f"PIN_INVENTORY_PREVIOUS_REVISION={cand}\n" in text
    assert "serves 100% and the pin file names it" in res.stderr
    assert "GET /items" in res.stderr or "/items -> HTTP 404" in res.stderr


def test_switch_still_requires_the_restock_probe(cloud):
    """Only rollback relaxes the restock probe: new code must have /admin/restock."""
    cand = prepared(cloud)
    assert cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST]).returncode == 0
    state = cloud.state
    state["http_rev"] = {cand: without(healthy_http(INV_URL), "GET /admin/restock")}
    cloud.set_state(state)
    res = cloud.run([REPIN, "switch"])
    assert res.returncode != 0
    assert "GET https://inv.example/admin/restock -> HTTP 404" in res.stderr


def test_write_pin_says_to_switch_immediately(cloud):
    cand = prepared(cloud)
    res = cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Run switch immediately; no reset/scenario in between" in res.stdout


def test_rollback_without_a_previous_pin_changes_nothing(cloud):
    res = cloud.run([REPIN, "rollback"])
    assert res.returncode != 0
    assert "nothing to roll back to" in res.stderr
    assert cloud.calls(["run", "services", "update-traffic"]) == []


def test_rollback_refuses_a_previous_pin_with_the_wrong_digest(cloud):
    write_pin(cloud.pin_file, OLD_PIN, OLD_DIGEST, "ata-inventory-api-00073-9vb", "sha256:" + "ef" * 32)
    res = cloud.run([REPIN, "rollback"])
    assert res.returncode != 0
    assert "traffic unchanged" in res.stderr


# --- status ---------------------------------------------------------------------------


def test_status_on_todays_live_shape_lists_every_gap(cloud):
    res = cloud.run([REPIN, "status"])

    assert res.returncode == 1
    out = res.stdout + res.stderr
    assert "Traffic: 100% on the pinned revision." in out
    assert "maxScale=100 (want 1)" in out
    assert "DB_USER is set (legacy alias; must be absent)" in out
    assert "ata-orders-api: fix with scripts/set-demo-instance-caps.sh --apply" in out
    assert cloud.calls(["run", "services", "update"]) == []  # read-only
    assert cloud.calls(["run", "services", "update-traffic"]) == []


def test_status_is_clean_after_caps_and_repin(cloud):
    cand = prepared(cloud)
    assert cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST]).returncode == 0
    assert cloud.run([REPIN, "switch"]).returncode == 0
    state = cloud.state
    for svc in state["services"].values():
        svc["service_max"] = "1"
    cloud.set_state(state)

    res = cloud.run([REPIN, "status"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert "on the pinned baseline" in res.stdout


@pytest.mark.parametrize("mode", ["MOCK_MODE", "DRY_RUN"])
def test_no_mock_or_dry_run_mode(cloud, mode):
    res = cloud.run([REPIN, "status"], **{mode: "true"})
    assert res.returncode != 0
    assert "no mock/dry-run mode" in res.stderr
    assert cloud.calls() == []


# --- first pin on a fresh project (--initial) -----------------------------------------


def test_initial_pin_on_a_fresh_project_end_to_end(tmp_path):
    cloud = FakeCloud(tmp_path, fresh_project_state())

    res = cloud.run([REPIN, "prepare", "--initial", "--image-digest", NEW_DIGEST])
    assert res.returncode == 0, res.stdout + res.stderr
    cand = candidate_name(cloud)
    assert cand not in [r for r, _ in cloud.serving()] and cloud.tags() == []
    assert f"write-pin --revision {cand} --digest {NEW_DIGEST} --initial" in res.stdout

    res = cloud.run([REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST, "--initial"])
    assert res.returncode == 0, res.stdout + res.stderr
    text = cloud.pin_file.read_text()
    assert f"PIN_INVENTORY_REVISION={cand}\n" in text
    assert "PIN_INVENTORY_PREVIOUS_REVISION=\n" in text  # nothing to roll back to here

    res = cloud.run([REPIN, "switch"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.serving() == [(cand, 100)]


def test_prepare_without_initial_on_a_fresh_project_points_to_initial(tmp_path):
    cloud = FakeCloud(tmp_path, fresh_project_state())
    res = cloud.run([REPIN, "prepare", "--image-digest", NEW_DIGEST])
    assert res.returncode != 0
    assert "prepare --initial" in res.stderr
    assert cloud.calls(["run", "services", "update"]) == []


@pytest.mark.parametrize("command", [
    ["prepare", "--initial", "--image-digest", NEW_DIGEST],
    ["write-pin", "--initial", "--revision", "ata-inventory-api-00001-abc", "--digest", NEW_DIGEST],
])
def test_initial_is_refused_when_a_pin_exists(cloud, command):
    before = cloud.pin_file.read_text()
    res = cloud.run([REPIN, *command])
    assert res.returncode != 0
    assert "--initial: a pin exists" in res.stderr
    assert cloud.calls(["run", "services", "update"]) == []
    assert cloud.pin_file.read_text() == before


def test_initial_refuses_when_it_cannot_tell_whether_a_pin_exists(tmp_path):
    state = fresh_project_state()
    state["fail"] = ["revisions-describe"]
    cloud = FakeCloud(tmp_path, state)
    res = cloud.run([REPIN, "prepare", "--initial", "--image-digest", NEW_DIGEST])
    assert res.returncode != 0
    assert "cannot tell whether" in res.stderr and "PERMISSION_DENIED" in res.stderr
    assert cloud.calls(["run", "services", "update"]) == []


# --- a new stack: no pin file for the prefix ------------------------------------------


def _new_stack(tmp_path, prefix="atat"):
    """A fresh project state for <prefix>, and a pin path for <prefix> that does not exist."""
    cloud = FakeCloud(tmp_path, for_prefix(fresh_project_state(), prefix))
    cloud.pin_file.unlink()
    cloud.pin_file = tmp_path / f"inventory-pin.{prefix}.env"
    return cloud


def _run_stack(cloud, argv, prefix="atat"):
    return cloud.run(argv, NAME_PREFIX=prefix, ATA_INVENTORY_PIN_FILE=str(cloud.pin_file))


def test_new_stack_initial_pin_creates_the_pin_file_end_to_end(tmp_path):
    cloud = _new_stack(tmp_path)

    res = _run_stack(cloud, [REPIN, "prepare", "--initial", "--image-digest", NEW_DIGEST])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "No pin file for stack 'atat'" in res.stdout
    update = cloud.calls(["run", "services", "update"])[0]
    assert update[3] == "atat-inventory-api"
    suffix = next(a.split("=", 1)[1] for a in update if a.startswith("--revision-suffix="))
    cand = f"atat-inventory-api-{suffix}"
    assert not cloud.pin_file.exists()  # prepare never writes the pin
    assert f"git add {cloud.pin_file}" in res.stdout

    res = _run_stack(cloud, [REPIN, "write-pin", "--revision", cand, "--digest", NEW_DIGEST, "--initial"])
    assert res.returncode == 0, res.stdout + res.stderr
    text = cloud.pin_file.read_text()
    assert f"PIN_INVENTORY_REVISION={cand}\n" in text and "PIN_INVENTORY_PREVIOUS_REVISION=\n" in text
    assert 'Stack "atat" (NAME_PREFIX=atat): atat-inventory-api baseline pin.' in text

    res = _run_stack(cloud, [REPIN, "switch"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert cloud.serving("atat-inventory-api") == [(cand, 100)]
    # Nothing ever named the ata stack.
    for call in cloud.calls() + cloud.curl_calls():
        assert not any("ata-" in a.replace("atat-", "") for a in call), call


@pytest.mark.parametrize("command", [["status"], ["switch"], ["rollback"],
                                     ["prepare", "--image-digest", NEW_DIGEST],
                                     ["write-pin", "--revision", "atat-inventory-api-00001-abc", "--digest", NEW_DIGEST]])
def test_new_stack_without_pin_file_points_to_prepare_initial(tmp_path, command):
    cloud = _new_stack(tmp_path)
    res = _run_stack(cloud, [REPIN, *command])
    assert res.returncode != 0
    assert "no inventory pin for stack 'atat'" in res.stderr and "prepare --initial" in res.stderr
    assert cloud.calls(["run", "services", "update"]) == [] and cloud.calls(["run", "services", "update-traffic"]) == []
    assert not cloud.pin_file.exists()


def test_status_for_a_new_prefix_uses_its_own_default_pin_file_and_leaves_atas_untouched(tmp_path):
    """Review R2: the REAL common.sh resolves the default pin path (no ATA_INVENTORY_PIN_FILE).
    With NAME_PREFIX=atat and only ata's pin file present, status must report the first-pin path
    for atat, never read or touch inventory-pin.ata.env, and make no change in the project."""
    import hashlib
    import shutil

    repo = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "scripts", repo / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "inventory-pin.*.env"))
    scenarios = repo / "scripts" / "scenarios"
    ata_pin = scenarios / "inventory-pin.ata.env"  # a fixture ata pin, never the committed one
    ata_pin.write_text(f"PIN_INVENTORY_REVISION=ata-inventory-api-pin-fixture\nPIN_INVENTORY_DIGEST=sha256:{'ab' * 32}\n")
    before = (hashlib.sha256(ata_pin.read_bytes()).hexdigest(), ata_pin.stat().st_mtime_ns)
    assert sorted(p.name for p in scenarios.glob("inventory-pin.*.env")) == ["inventory-pin.ata.env"]

    (tmp_path / "cloud").mkdir()
    cloud = FakeCloud(tmp_path / "cloud", for_prefix(fresh_project_state(), "atat"))
    res = cloud.run([repo / "scripts" / "repin-inventory.sh", "status"], NAME_PREFIX="atat", ATA_INVENTORY_PIN_FILE="")

    assert res.returncode != 0
    assert f"no inventory pin for stack 'atat': {scenarios / 'inventory-pin.atat.env'} does not exist" in res.stderr
    assert "prepare --initial" in res.stderr
    assert "inventory-pin.ata.env" not in res.stdout + res.stderr
    assert (hashlib.sha256(ata_pin.read_bytes()).hexdigest(), ata_pin.stat().st_mtime_ns) == before
    assert not (scenarios / "inventory-pin.atat.env").exists()
    assert cloud.calls(["run", "services", "update"]) == [] and cloud.calls(["run", "services", "update-traffic"]) == []
