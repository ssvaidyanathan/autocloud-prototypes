"""reset-scenarios.sh step 6 waits for every incident group to reach a terminal state
before the quiet period and the step-7 archive.

QA S4: the reset archived the incident files while a group was still cooling_down, so the
agent's recovery note (posted when the 180s cool-down ends) raced the archive. The fix is
a condition, not a longer quiet period: after the alerts close, step 6 polls the OKF
`status` of every file in knowledge/incident-manager/ (read with gcloud storage, as in
step 7) until all are terminal, bounded by --timeout; on timeout it fails and lists the
active groups. --no-wait skips the check (documented as risky).

A fake `gcloud` serves `gcloud storage ls/cat/mv` from a local directory. A file
`<root>/_seq/<name>` holds one status per line; each `cat` of <name> rewrites the file's
status to the next line (the last one repeats), so a group can change state between
polls. The Monitoring alert query is stubbed to "no open alerts". reset-scenarios.sh is
sourced (which defines its functions without running the reset) and run under
ATA_TEST_BASH, as in test_bash32_compat.py.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent.app.grouping.models import IncidentGroupStatus, OKFDocument

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
RESET_SCRIPT = SCRIPTS_DIR / "reset-scenarios.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
BUCKET = "proj-ata-incidents"
PREFIX = "knowledge/incident-manager"
AUTH_ERROR = "HTTPError 401: Invalid Credentials"

FAKE_GCLOUD = '''#!{python} -I
import fnmatch, os, re, shutil, sys

args = sys.argv[1:]
with open(os.environ["ATA_TEST_CALLS"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\\n")
if args[:1] != ["storage"]:
    sys.exit(0)
cmd, urls = args[1], args[2:]
root = os.environ["ATA_FAKE_GCS_ROOT"]
if cmd in os.environ.get("ATA_FAKE_GCS_FAIL", "").split(","):
    sys.stderr.write("ERROR: (gcloud.storage.%s) {auth_error}\\n" % cmd)
    sys.exit(1)

def local(url):
    return os.path.join(root, url[len("gs://"):])

def matches(url):
    folder, pattern = os.path.split(local(url))
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, n) for n in os.listdir(folder)
                  if os.path.isfile(os.path.join(folder, n)) and fnmatch.fnmatchcase(n, pattern))

def no_match(url):
    sys.stderr.write("ERROR: (gcloud.storage.%s) One or more URLs matched no objects.\\n" % cmd)
    sys.exit(1)

def advance(path):
    seq = os.path.join(root, "_seq", os.path.basename(path))
    if not os.path.exists(seq):
        return
    states = open(seq).read().split()
    if len(states) > 1:
        open(seq, "w").write("\\n".join(states[1:]) + "\\n")
    text = open(path).read()
    open(path, "w").write(re.sub(r"(?m)^status: .*$", "status: " + states[0], text, count=1))

if cmd == "mv":
    found = matches(urls[0]) or no_match(urls[0])
    dst = local(urls[-1])
    os.makedirs(dst, exist_ok=True)
    for p in found:
        shutil.move(p, os.path.join(dst, os.path.basename(p)))
    sys.exit(0)
found = matches(urls[0]) or no_match(urls[0])
for p in found:
    if cmd == "ls":
        print("gs://" + os.path.relpath(p, root))
    elif cmd == "cat":
        advance(p)
        sys.stdout.write(open(p).read())
'''


@pytest.fixture
def gcs(tmp_path):
    """(stub bin dir, local bucket root); the bucket starts empty."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to(TEST_BASH)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.format(python=sys.executable, auth_error=AUTH_ERROR))
    gcloud.chmod(0o755)
    root = tmp_path / "gcs"
    (root / BUCKET).mkdir(parents=True)
    return bin_dir, root


def okf(group_id, status):
    """An incident file exactly as the agent writes it (OKFDocument.to_markdown)."""
    return OKFDocument(group_id=group_id, status=status, policy_name="ata-inventory-5xx",
                       alerts=[{"incident_id": "i-1", "state": "closed"}],
                       body="# Incident\n\n- Status: open\n").to_markdown()


def put(root, name, text, sequence=None):
    path = root / BUCKET / PREFIX / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if sequence:
        seq = root / "_seq" / name
        seq.parent.mkdir(parents=True, exist_ok=True)
        seq.write_text("\n".join(sequence) + "\n")


def run_step6(tmp_path, gcs, after="", fail="", quiet=0, timeout=900, poll=0, **extra):
    """Source the reset, stub the alert query to 0 open alerts, run step 6 (then `after`)."""
    bin_dir, root = gcs
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "set -euo pipefail\n"
        f'source "{RESET_SCRIPT}"\n'
        f"POLL_INTERVAL_SECS={poll}\nQUIET_PERIOD_SECS={quiet}\nWAIT_TIMEOUT_SECS={timeout}\n"
        "get_serving_revisions() { echo ''; }\n"
        "resolve_monitoring_token() { echo tok; }\n"
        """query_open_ata_alerts() { echo '{"active": [], "ignored": []}'; }\n"""
        "wait_for_open_alerts\n"
        "echo STEP6_DONE\n"
        f"{after}\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("ATA_API_KEY", "INCIDENTS_BUCKET", "ATA_RESET_NO_WAIT", "NO_WAIT")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", ATA_TEST_CALLS=str(tmp_path / "calls"),
               ATA_FAKE_GCS_ROOT=str(root), ATA_FAKE_GCS_FAIL=fail,
               MOCK_MODE="false", DRY_RUN="false", PROJECT_ID="proj")
    env.update(extra)
    return subprocess.run([TEST_BASH, str(probe)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=60, check=False)


def calls(tmp_path, verb=None):
    log = tmp_path / "calls"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line for line in lines if verb is None or line.startswith(f"gcloud storage {verb} ")]


def active_names(root):
    folder = root / BUCKET / PREFIX
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def out(res):
    return res.stdout + res.stderr


# --- terminal immediately ---------------------------------------------------------


def test_terminal_groups_pass_at_once_and_are_then_archived(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.RECOVERED))
    put(root, "incident-b.md", okf("b", IncidentGroupStatus.RESOLVED))
    put(root, "incident-c.md", okf("c", IncidentGroupStatus.DEGRADED))

    res = run_step6(tmp_path, gcs, after="archive_active_incidents || exit 1")

    assert res.returncode == 0, out(res)
    assert "STEP6_DONE" in res.stdout
    assert "not terminal yet" not in res.stdout
    assert "all incident groups terminal" in res.stdout
    assert calls(tmp_path, "ls") == [f"gcloud storage ls gs://{BUCKET}/{PREFIX}/*.md"] * 2
    assert len(calls(tmp_path, "cat")) == 3
    assert active_names(root) == []


def test_no_incident_files_passes(tmp_path, gcs):
    res = run_step6(tmp_path, gcs)

    assert res.returncode == 0, out(res)
    assert "STEP6_DONE" in res.stdout
    assert calls(tmp_path, "cat") == []


# --- becomes terminal after N polls -------------------------------------------------


def test_waits_until_the_cooling_group_is_recovered_then_archives(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN),
        sequence=["cooling_down", "cooling_down", "recovered"])
    put(root, "incident-b.md", okf("b", IncidentGroupStatus.RECOVERED))

    res = run_step6(tmp_path, gcs, after="archive_active_incidents || exit 1")

    assert res.returncode == 0, out(res)
    progress = [line for line in res.stdout.splitlines() if "not terminal yet" in line]
    assert len(progress) == 2
    assert all("1 incident group(s) not terminal yet [incident-a.md (cooling_down)]" in line for line in progress)
    assert "incident-b.md" not in "".join(progress)
    assert len([c for c in calls(tmp_path, "cat") if c.endswith("incident-a.md")]) == 3
    # The archive runs only after the wait, so the archived file carries the final status.
    assert "status: recovered" in (root / BUCKET / "archive" / "incident-a.md").read_text()
    assert active_names(root) == []


def test_waits_through_every_active_status_the_agent_writes(tmp_path, gcs):
    _, root = gcs
    for status in ("open", "investigating", "ticketed", "mitigation-in-progress"):
        put(root, f"incident-{status}.md", okf(status, status), sequence=[status, "recovered"])

    res = run_step6(tmp_path, gcs)

    assert res.returncode == 0, out(res)
    progress = [line for line in res.stdout.splitlines() if "not terminal yet" in line]
    assert len(progress) == 1
    for status in ("open", "investigating", "ticketed", "mitigation-in-progress"):
        assert f"incident-{status}.md ({status})" in progress[0]


def test_quiet_period_starts_only_once_groups_are_terminal_and_restarts_on_regression(tmp_path, gcs):
    """A group back in cooling during the quiet period (alert reopened and closed) restarts it."""
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN),
        sequence=["cooling_down", "recovered", "cooling_down", "recovered"])

    res = run_step6(tmp_path, gcs, quiet=1, poll=1)

    assert res.returncode == 0, out(res)
    lines = res.stdout.splitlines()
    events = [("wait" if "not terminal yet" in line else "quiet")
              for line in lines if "not terminal yet" in line or "Entering 1s quiet period" in line]
    assert events == ["wait", "quiet", "wait", "quiet"]
    assert "Quiet period complete" in res.stdout


# --- timeout -> loud failure --------------------------------------------------------


def test_timeout_fails_loudly_lists_active_groups_and_does_not_archive(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN))
    put(root, "incident-b.md", okf("b", IncidentGroupStatus.TICKETED))
    put(root, "incident-c.md", okf("c", IncidentGroupStatus.RECOVERED))

    res = run_step6(tmp_path, gcs, timeout=0, after="archive_active_incidents || exit 1")

    assert res.returncode == 1, out(res)
    assert "STEP6_DONE" not in res.stdout
    assert "Timed out after 0s" in res.stderr
    assert "2 incident group(s) are not in a terminal state" in res.stderr
    assert "  - incident-a.md (cooling_down)" in res.stderr
    assert "  - incident-b.md (ticketed)" in res.stderr
    assert "incident-c.md" not in res.stderr
    assert "NOT archived" in res.stderr
    assert "--no-wait" in res.stderr
    assert calls(tmp_path, "mv") == []
    assert active_names(root) == ["incident-a.md", "incident-b.md", "incident-c.md"]


@pytest.mark.parametrize("text, shown", [
    ("---\ntype: incident\ngroup_id: x\n---\n\n- status: recovered\n", "no status"),
    ("no frontmatter\nstatus: recovered\n", "no status"),
    ("---\ngroup_id: x\nstatus: stable\n---\n", "stable"),
    ("---\ngroup_id: x\nstatus: something-new\n---\n", "something-new"),
])
def test_missing_or_unknown_status_is_not_terminal(tmp_path, gcs, text, shown):
    _, root = gcs
    put(root, "incident-x.md", text)

    res = run_step6(tmp_path, gcs, timeout=0)

    assert res.returncode == 1, out(res)
    assert f"  - incident-x.md ({shown})" in res.stderr


# --- gcloud storage failure ---------------------------------------------------------


@pytest.mark.parametrize("fail", ["ls", "cat"])
def test_unreadable_incident_files_fail_loudly_and_do_not_archive(tmp_path, gcs, fail):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.RECOVERED))

    res = run_step6(tmp_path, gcs, fail=fail, after="archive_active_incidents || exit 1")

    assert res.returncode == 1, out(res)
    assert AUTH_ERROR in res.stderr
    assert "could not read incident group states" in res.stderr
    assert "gcloud auth list" in res.stderr
    assert "--no-wait" in res.stderr
    assert calls(tmp_path, "mv") == []
    assert active_names(root) == ["incident-a.md"]


# --- --no-wait ----------------------------------------------------------------------


@pytest.mark.parametrize("env", [{"ATA_RESET_NO_WAIT": "true"}, {"NO_WAIT": "true"}])
def test_no_wait_skips_the_group_check_and_archives_active_groups(tmp_path, gcs, env):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN))

    res = run_step6(tmp_path, gcs, after="archive_active_incidents || exit 1", **env)

    assert res.returncode == 0, out(res)
    assert "[--no-wait]" in res.stdout
    assert "still working on a group" in out(res)
    assert calls(tmp_path, "cat") == []
    assert active_names(root) == []


# --- status parsing -----------------------------------------------------------------


def okf_status(tmp_path, text):
    probe = tmp_path / "parse.sh"
    probe.write_text(f'source "{RESET_SCRIPT}"\nokf_status < "$OKF_DOC"\n')
    data = tmp_path / "doc.md"
    data.write_bytes(text.encode())
    res = subprocess.run([TEST_BASH, str(probe)], capture_output=True, text=True, timeout=30,
                         check=False, env={**os.environ, "MOCK_MODE": "true", "OKF_DOC": str(data)})
    assert res.returncode == 0, out(res)
    return res.stdout.strip()


@pytest.mark.parametrize("status", [s.value for s in IncidentGroupStatus])
def test_okf_status_reads_every_status_the_agent_serializes(tmp_path, status):
    assert okf_status(tmp_path, okf("g", status)) == status


def test_okf_status_reads_the_pre_analysis_lock_file_format(tmp_path):
    """triage.py writes its lock file with yaml.dump, status mitigation-in-progress."""
    header = yaml.dump({"type": "incident", "group_id": "g", "status": "mitigation-in-progress",
                        "alerts": [{"incident_id": "i", "state": "open"}]}, sort_keys=False)
    text = f"---\n{header.strip()}\n---\n\n# Incident\n- Status: mitigation-in-progress\n"
    assert okf_status(tmp_path, text) == "mitigation-in-progress"


@pytest.mark.parametrize("text, expected", [
    ("---\r\ngroup_id: g\r\nstatus: recovered\r\n---\r\n", "recovered"),
    ("---\ngroup_id: g\nstatus: 'cooling_down'\n---\n", "cooling_down"),
    ('---\ngroup_id: g\nstatus: "resolved"  \n---\n', "resolved"),
    ("---\ngroup_id: g\ncorrelation:\n  status: recovered\n---\nstatus: recovered\n", ""),
])
def test_okf_status_edge_cases(tmp_path, text, expected):
    assert okf_status(tmp_path, text) == expected


# --- wiring and defaults ------------------------------------------------------------


def test_main_flow_waits_before_archiving():
    text = RESET_SCRIPT.read_text()
    main = text[text.index('log_info "=== Starting Scenario Fault Cleanup'):]
    assert main.index("\nwait_for_open_alerts\n") < main.index("archive_active_incidents || exit 1")


def test_help_documents_the_group_wait_no_wait_risk_and_180s_default():
    res = subprocess.run([str(RESET_SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
    assert re.search(r"--no-wait .*terminal incident groups", res.stdout)
    assert "risky" in res.stdout
    assert "(default: 180s / 3m)" in res.stdout
