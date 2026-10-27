"""Tests for S5 traffic routing and the inventory baseline restore (stubbed gcloud).

Inventory traffic is pinned to PINNED_INVENTORY_REVISION by name. Cloud Run keeps a
named-revision split for new revisions, so the revision S5 creates gets 0% unless S5
routes traffic to it. Reset and the S5 revert must restore the pin, and must skip
`services update` (which always creates a revision) when the template already matches.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIOS_DIR = Path(__file__).parent.parent / "scripts" / "scenarios"
S5_SCRIPT = SCENARIOS_DIR / "s5_bad_deploy.sh"
PINNED_DIGEST = "sha256:24b07eaa50db6d226889607116f0dd43693df5e1f597867a79e0f86e3d7767ce"
PINNED_REVISION = "ata-inventory-api-00073-9vb"
PINNED_IMAGE = f"us-west1-docker.pkg.dev/cloud-assist-fde-2/ata-docker/ata-inventory-api@{PINNED_DIGEST}"
GOOD_USER = "ata-inventory-sa@cloud-assist-fde-2.iam"
BAD_USER = "invalid-iam-user@cloud-assist-fde-2.iam"

# Stateful fake gcloud: state.json holds traffic, template and revision counter;
# calls.log records one JSON argv per invocation. "fail" lists verbs to fail.
FAKE_GCLOUD = r'''
import json, sys
from pathlib import Path

here = Path(__file__).parent
state_path = here / "state.json"
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with open(here / "calls.log", "a") as log:
    log.write(json.dumps(args) + "\n")

def opt(name):
    for a in args:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None

def save():
    state_path.write_text(json.dumps(state))

fmt = opt("format") or ""
if args[:2] == ["auth", "print-identity-token"]:
    print("fake-token")
elif args[:3] == ["run", "revisions", "describe"]:
    rev = args[3]
    if rev in state["revisions"]:
        print(f"img@{state['revisions'][rev]}")
    else:
        sys.exit(1)
elif args[:3] == ["run", "services", "describe"]:
    if "describe" in state["fail"]:
        sys.exit(1)
    if fmt == "value(status.latestCreatedRevisionName)":
        print(state["latest"])
    elif fmt == "value(status.url)":
        print("https://inv.example")
    elif fmt.startswith("csv[no-heading]"):
        revs = ";".join(r for r, _ in state["traffic"])
        pcts = ";".join(str(p) for _, p in state["traffic"])
        print(f"{revs},{pcts}")
    elif fmt == "json":
        env = [{"name": k, "value": v} for k, v in state["env"].items()]
        print(json.dumps({"spec": {"template": {"spec": {"containers": [{"image": state["image"], "env": env}]}}}}))
    else:
        sys.exit(f"fake gcloud: unhandled describe format {fmt}")
elif args[:3] == ["run", "services", "update"]:
    if "update" in state["fail"]:
        sys.exit(1)
    state["image"] = opt("image")
    state["env"].update(dict(kv.split("=", 1) for kv in opt("update-env-vars").split(",")))
    if "no-new-revision" not in state["fail"]:
        state["counter"] += 1
        suffix = opt("revision-suffix") or f"{state['counter']:05d}-new"
        state["latest"] = f"ata-inventory-api-{suffix}"
        state["revisions"][state["latest"]] = state["digest_for_new"]
    if "concurrent-writer" in state["fail"]:
        # Another writer's `services update` lands right after ours.
        state["counter"] += 1
        state["latest"] = f"ata-inventory-api-{state['counter']:05d}-other"
        state["revisions"][state["latest"]] = state["digest_for_new"]
    save()
elif args[:3] == ["run", "services", "update-traffic"]:
    if "update-traffic" in state["fail"]:
        sys.exit(1)
    if "ignore-traffic" not in state["fail"]:
        rev, pct = opt("to-revisions").split("=")
        state["traffic"] = [[rev, int(pct)]]
        save()
else:
    sys.exit(f"fake gcloud: unhandled {args}")
'''


def baseline_state(**overrides):
    state = {
        "traffic": [[PINNED_REVISION, 100]],
        "latest": "ata-inventory-api-00139-ff2",
        "counter": 139,
        "image": PINNED_IMAGE,
        "env": {
            "ATA_INVENTORY_DB_USER": GOOD_USER,
            "DB_USER": GOOD_USER,
            "ATA_INVENTORY_MOCK_DB": "false",
            "MOCK_DB": "false",
            "ATA_INVENTORY_DB_NAME": "inventory",
        },
        "revisions": {PINNED_REVISION: PINNED_DIGEST},
        "digest_for_new": PINNED_DIGEST,
        "fail": [],
    }
    state.update(overrides)
    return state


class Env:
    def __init__(self, tmp_path, state):
        self.dir = tmp_path / "bin"
        self.dir.mkdir()
        (self.dir / "state.json").write_text(json.dumps(state))
        (self.dir / "gcloud.py").write_text(FAKE_GCLOUD)
        gcloud = self.dir / "gcloud"
        gcloud.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" -I "{self.dir}/gcloud.py" "$@"\n')
        gcloud.chmod(0o755)
        curl = self.dir / "curl"
        curl.write_text("#!/usr/bin/env bash\necho 500\n")
        curl.chmod(0o755)
        self.tmp_path = tmp_path

    def run(self, argv):
        env = os.environ.copy()
        for var in ("PINNED_INVENTORY_DIGEST", "PINNED_INVENTORY_REVISION", "PINNED_INVENTORY_IMAGE",
                    "PROJECT_ID", "GCP_PROJECT", "REGION", "GCP_REGION", "NAME_PREFIX"):
            env.pop(var, None)
        env.update(PATH=f"{self.dir}{os.pathsep}{env['PATH']}", MOCK_MODE="false", DRY_RUN="false",
                   ATA_API_KEY="test-key")
        return subprocess.run(argv, cwd=str(self.tmp_path), capture_output=True, text=True,
                              env=env, check=False, timeout=30)

    def run_function(self, body):
        script = self.tmp_path / "fn.sh"
        script.write_text(f'#!/usr/bin/env bash\nsource "{SCENARIOS_DIR}/common.sh"\n{body}\n')
        script.chmod(0o755)
        return self.run([str(script)])

    @property
    def state(self):
        return json.loads((self.dir / "state.json").read_text())

    @property
    def calls(self):
        log = self.dir / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def writes(self):
        return [c[:3] for c in self.calls if c[:3] in (["run", "services", "update"],
                                                        ["run", "services", "update-traffic"])]


# --- S5 injection --------------------------------------------------------------


def s5_revision(env):
    """Revision name S5 asked for via --revision-suffix on its services update."""
    update = next(c for c in env.calls if c[:3] == ["run", "services", "update"])
    suffix = next(a.split("=", 1)[1] for a in update if a.startswith("--revision-suffix="))
    return f"ata-inventory-api-{suffix}"


def test_s5_routes_all_traffic_to_the_new_faulty_revision(tmp_path):
    env = Env(tmp_path, baseline_state())

    res = env.run([str(S5_SCRIPT), "--hold", "0s"])

    assert res.returncode == 0, res.stdout + res.stderr
    new_rev = s5_revision(env)
    assert new_rev.startswith("ata-inventory-api-s5-")
    assert env.state["traffic"] == [[new_rev, 100]]
    assert env.state["env"]["DB_USER"] == BAD_USER
    assert env.writes() == [["run", "services", "update"], ["run", "services", "update-traffic"]]
    assert f"--to-revisions={new_rev}=100" in env.calls[[c[:3] for c in env.calls].index(
        ["run", "services", "update-traffic"])]
    assert f"{new_rev} is serving 100%" in res.stdout
    assert "Injected Successfully" in res.stdout


@pytest.mark.parametrize(
    "fail, message",
    [
        ("update", "Cloud Run update of ata-inventory-api failed"),
        ("no-new-revision", "no new ata-inventory-api revision"),
        ("update-traffic", "could not route traffic to ata-inventory-api-s5-"),
        ("ignore-traffic", "is not serving 100% of traffic"),
        # S5 no longer describes before updating; a describe failure trips the serving check.
        ("describe", "ata-inventory-api traffic is '<unknown>'"),
    ],
)
def test_s5_fails_loudly_instead_of_driving_traffic_at_the_pinned_revision(tmp_path, fail, message):
    env = Env(tmp_path, baseline_state(fail=[fail]))

    res = env.run([str(S5_SCRIPT), "--hold", "0s"])

    assert res.returncode != 0
    assert message in res.stderr
    assert "Injected Successfully" not in res.stdout
    assert "sustained traffic loop" not in res.stdout


def test_s5_routes_to_its_own_revision_despite_a_concurrent_writer(tmp_path):
    """A concurrent `services update` must not make S5 route to the other writer's revision."""
    env = Env(tmp_path, baseline_state(fail=["concurrent-writer"]))

    res = env.run([str(S5_SCRIPT), "--hold", "0s"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.state["latest"].endswith("-other")
    assert env.state["traffic"] == [[s5_revision(env), 100]]
    assert not any(c[:3] == ["run", "services", "describe"] and
                   "--format=value(status.latestCreatedRevisionName)" in c for c in env.calls)


def test_s5_update_failure_does_not_attempt_traffic_routing(tmp_path):
    env = Env(tmp_path, baseline_state(fail=["update"]))

    env.run([str(S5_SCRIPT), "--hold", "0s"])

    assert ["run", "services", "update-traffic"] not in env.writes()
    assert env.state["traffic"] == [[PINNED_REVISION, 100]]


@pytest.mark.parametrize("mode", ["--mock", "--dry-run"])
def test_s5_mock_and_dry_run_make_no_gcloud_calls(tmp_path, mode):
    env = Env(tmp_path, baseline_state())

    res = env.run([str(S5_SCRIPT), mode, "--hold", "0s"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.calls == []


# --- S5 revert / reset restore -------------------------------------------------


def s5_active_state(**overrides):
    state = baseline_state(
        traffic=[["ata-inventory-api-00140-new", 100]],
        latest="ata-inventory-api-00140-new",
        counter=140,
    )
    state["env"].update(ATA_INVENTORY_DB_USER=BAD_USER, DB_USER=BAD_USER)
    state["revisions"]["ata-inventory-api-00140-new"] = PINNED_DIGEST
    state.update(overrides)
    return state


def test_revert_after_s5_restores_pin_and_template(tmp_path):
    env = Env(tmp_path, s5_active_state())

    res = env.run([str(S5_SCRIPT), "--revert"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.state["traffic"] == [[PINNED_REVISION, 100]]
    assert env.state["env"]["DB_USER"] == GOOD_USER
    assert env.state["env"]["ATA_INVENTORY_DB_USER"] == GOOD_USER
    # Traffic is re-pinned before the template update, so the new revision gets 0%.
    assert env.writes() == [["run", "services", "update-traffic"], ["run", "services", "update"]]


def test_revert_full_s5_cycle_ends_on_pinned_revision(tmp_path):
    env = Env(tmp_path, baseline_state())

    assert env.run([str(S5_SCRIPT), "--hold", "0s"]).returncode == 0
    res = env.run([str(S5_SCRIPT), "--revert"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.state["traffic"] == [[PINNED_REVISION, 100]]
    assert env.state["env"]["DB_USER"] == GOOD_USER


def test_restore_on_baseline_makes_no_writes(tmp_path):
    """Reset runs every time; on a healthy baseline it must not create a revision."""
    env = Env(tmp_path, baseline_state())

    res = env.run_function("restore_inventory_pinned_baseline")

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.writes() == []
    assert env.state["latest"] == "ata-inventory-api-00139-ff2"
    assert "skipping services update" in res.stdout


def test_restore_repins_traffic_without_template_update_when_template_matches(tmp_path):
    env = Env(tmp_path, baseline_state(traffic=[["ata-inventory-api-00140-new", 100]]))

    res = env.run_function("restore_inventory_pinned_baseline")

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.writes() == [["run", "services", "update-traffic"]]
    assert env.state["traffic"] == [[PINNED_REVISION, 100]]


@pytest.mark.parametrize(
    "field, value",
    [("image", "us-west1-docker.pkg.dev/x/ata-docker/ata-inventory-api@sha256:other"), ("MOCK_DB", "true")],
)
def test_restore_updates_template_when_it_differs(tmp_path, field, value):
    state = baseline_state()
    if field == "image":
        state["image"] = value
    else:
        state["env"][field] = value
    env = Env(tmp_path, state)

    res = env.run_function("restore_inventory_pinned_baseline")

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.writes() == [["run", "services", "update"]]
    assert env.state["image"] == PINNED_IMAGE
    assert env.state["env"]["MOCK_DB"] == "false"
    assert env.state["traffic"] == [[PINNED_REVISION, 100]]


@pytest.mark.parametrize(
    "fail, message",
    [
        ("update-traffic", f"Failed to route ata-inventory-api traffic to {PINNED_REVISION}"),
        ("update", "Failed to restore the ata-inventory-api service template"),
        ("ignore-traffic", f"expected {PINNED_REVISION}=100"),
    ],
)
def test_revert_fails_loudly(tmp_path, fail, message):
    env = Env(tmp_path, s5_active_state(fail=[fail]))

    res = env.run([str(S5_SCRIPT), "--revert"])

    assert res.returncode != 0
    assert message in res.stderr
    assert "S5 revert failed" in res.stderr


def test_restore_fails_loudly_when_template_unreadable(tmp_path):
    env = Env(tmp_path, baseline_state(fail=["describe"]))

    res = env.run_function("restore_inventory_pinned_baseline")

    assert res.returncode != 0
    assert env.writes() == [["run", "services", "update-traffic"]]  # describe failed -> traffic unknown
    assert "Could not read the ata-inventory-api service template" in res.stderr


def test_restore_refuses_when_pinned_revision_runs_wrong_digest(tmp_path):
    env = Env(tmp_path, s5_active_state(revisions={PINNED_REVISION: "sha256:old"}))

    res = env.run([str(S5_SCRIPT), "--revert"])

    assert res.returncode != 0
    assert env.writes() == []
    assert f"expected {PINNED_DIGEST}" in res.stderr


def test_revert_mock_flag_makes_no_gcloud_calls(tmp_path):
    env = Env(tmp_path, s5_active_state())

    res = env.run([str(S5_SCRIPT), "--mock", "--revert"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.calls == []


def test_revert_with_mock_env_makes_no_gcloud_calls(tmp_path):
    env = Env(tmp_path, s5_active_state())
    script = tmp_path / "revert_mock.sh"
    script.write_text(f'#!/usr/bin/env bash\nMOCK_MODE=true exec "{S5_SCRIPT}" --revert\n')
    script.chmod(0o755)

    res = env.run([str(script)])

    assert res.returncode == 0, res.stdout + res.stderr
    assert env.calls == []
    assert "[MOCK/DRY-RUN]" in res.stdout


# --- Guards while S5 is active -------------------------------------------------


def test_preflight_serving_guard_during_s5_fails_with_revert_hint(tmp_path):
    env = Env(tmp_path, s5_active_state())

    res = env.run_function("verify_inventory_serving_pinned")

    assert res.returncode != 0
    assert f"expected {PINNED_REVISION}=100" in res.stderr
    assert "s5_bad_deploy.sh --revert" in res.stderr


def test_digest_guard_is_independent_of_s5_traffic(tmp_path):
    """reset-scenarios.sh runs the digest guard first; S5 traffic must not trip it."""
    env = Env(tmp_path, s5_active_state())

    res = env.run_function("verify_pinned_inventory_revision")

    assert res.returncode == 0, res.stdout + res.stderr
