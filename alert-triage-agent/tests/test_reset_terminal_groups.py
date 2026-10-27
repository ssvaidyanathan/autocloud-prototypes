"""reset-scenarios.sh step 6 waits for every incident group to reach a terminal state
before the step-7 archive, concurrently with the quiet period.

QA S4: the reset archived the incident files while a group was still cooling_down, so the
agent's recovery note (posted when the 180s cool-down ends) raced the archive. The fix is
a condition, not a longer quiet period: after the alerts close, step 6 polls the OKF
`status` of every file in knowledge/incident-manager/ (read with gcloud storage, as in
step 7) until all are terminal, bounded by --timeout; on timeout it fails and lists the
active groups. --no-wait skips the check (documented as risky).

A fake `gcloud` serves `gcloud storage ls/cat/mv` from a local directory. A file
`<root>/_seq/<name>` holds one status per line; each `cat` of <name> rewrites the file's
status to the next line (the last one repeats), so a group can change state between
polls. "recovered" is the agent's final state (status recovered with
recovery_note_posted_at set); "recovered-claimed" is the window between the agent's claim
and its note post (status recovered, recovery_note_posted_at null). The Monitoring alert query is stubbed to "no open alerts", or to a per-poll sequence
of open-alert counts. reset-scenarios.sh is sourced (which defines its functions without
running the reset) and run under ATA_TEST_BASH, as in test_bash32_compat.py.

Ordering (round 4): the quiet period starts when the alerts close and runs while the reset
waits for terminal groups; the archive follows when both are done, so the wait after the
close is max(quiet period, time to terminal), not the sum. The ordering tests use a fake
clock: `date +%s` and `sleep` are shell functions over FAKE_NOW (starts at 1000).
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
    status, note = states[0], None
    if status == "recovered":
        note = "'2026-10-27T10:03:14Z'"
    elif status == "recovered-claimed":
        status, note = "recovered", "null"
    text = re.sub(r"(?m)^status: .*$", "status: " + status, text, count=1)
    if note is not None:
        text = re.sub(r"(?m)^recovery_note_posted_at: .*$", "recovery_note_posted_at: " + note, text, count=1)
    open(path, "w").write(text)

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


NOTE_POSTED_AT = "2026-10-27T10:03:14Z"


def okf(group_id, status, note_posted_at="default"):
    """An incident file exactly as the agent writes it (OKFDocument.to_markdown). A
    'recovered' group gets recovery_note_posted_at by default, as the agent leaves it once
    the recovery note is posted; pass note_posted_at=None for the claim-to-note window."""
    if note_posted_at == "default":
        note_posted_at = NOTE_POSTED_AT if status == IncidentGroupStatus.RECOVERED else None
    return OKFDocument(group_id=group_id, status=status, policy_name="ata-inventory-5xx",
                       alerts=[{"incident_id": "i-1", "state": "closed"}],
                       recovery_note_posted_at=note_posted_at,
                       body="# Incident\n\n- Status: open\n").to_markdown()


def put(root, name, text, sequence=None):
    path = root / BUCKET / PREFIX / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if sequence:
        seq = root / "_seq" / name
        seq.parent.mkdir(parents=True, exist_ok=True)
        seq.write_text("\n".join(sequence) + "\n")


# Fake clock: `date +%s` prints FAKE_NOW and `sleep N` advances it (other `date` calls
# use the real one). `sleep` runs in the main shell, `date` in $(...) reads the variable.
FAKE_CLOCK = (
    "FAKE_NOW=1000\n"
    'date() { if [ "$*" = "+%s" ]; then echo "${FAKE_NOW}"; else command date "$@"; fi; }\n'
    'sleep() { FAKE_NOW=$((FAKE_NOW + $1)); }\n'
)

# Open-alert counts, one per query (the last one repeats), read from $ATA_TEST_ALERTS.
ALERT_SEQUENCE_STUB = """query_open_ata_alerts() {
    local n i out=""
    n="$(head -n 1 "${ATA_TEST_ALERTS}")"
    if [ "$(wc -l < "${ATA_TEST_ALERTS}")" -gt 1 ]; then
        tail -n +2 "${ATA_TEST_ALERTS}" > "${ATA_TEST_ALERTS}.next"
        mv "${ATA_TEST_ALERTS}.next" "${ATA_TEST_ALERTS}"
    fi
    i=0
    while [ "${i}" -lt "${n}" ]; do
        i=$((i + 1))
        out="${out}${out:+, }{\\"id\\": \\"a${i}\\", \\"policy_display\\": \\"ata-inventory-5xx\\"}"
    done
    echo "{\\"active\\": [${out}], \\"ignored\\": []}"
}
"""


def run_step6(tmp_path, gcs, after="", fail="", quiet=0, timeout=900, poll=0,
              alerts=None, fake_clock=False, pre="", **extra):
    """Source the reset, stub the alert query (0 open alerts, or the per-poll counts in
    `alerts`), optionally the clock, and run step 6 (then `after`)."""
    bin_dir, root = gcs
    if alerts is None:
        alert_stub = """query_open_ata_alerts() { echo '{"active": [], "ignored": []}'; }\n"""
    else:
        (tmp_path / "alerts").write_text("".join(f"{n}\n" for n in alerts))
        extra.setdefault("ATA_TEST_ALERTS", str(tmp_path / "alerts"))
        alert_stub = ALERT_SEQUENCE_STUB
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "set -euo pipefail\n"
        f'source "{RESET_SCRIPT}"\n'
        f"POLL_INTERVAL_SECS={poll}\nQUIET_PERIOD_SECS={quiet}\nWAIT_TIMEOUT_SECS={timeout}\n"
        + pre
        + (FAKE_CLOCK if fake_clock else "")
        + "get_serving_revisions() { echo ''; }\n"
        "resolve_monitoring_token() { echo tok; }\n"
        + alert_stub
        + "wait_for_open_alerts\n"
        "echo STEP6_DONE\n"
        + ('echo "DONE_AT=${FAKE_NOW}"\n' if fake_clock else "")
        + f"{after}\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("ATA_API_KEY", "INCIDENTS_BUCKET", "ATA_RESET_NO_WAIT", "NO_WAIT",
                        "ATA_RESET_IGNORE_GROUPS")}
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


def done_at(res):
    """Fake-clock seconds since the start (1000) at which step 6 returned."""
    match = re.search(r"^DONE_AT=(\d+)$", res.stdout, re.M)
    assert match, out(res)
    return int(match.group(1)) - 1000


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


# --- ordering: quiet period and terminal-state wait run concurrently ----------------


def test_terminal_before_the_quiet_period_ends_archives_at_the_quiet_end(tmp_path, gcs):
    """Group recovered 30s after the close; the 180s quiet period still runs to its end."""
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN),
        sequence=["cooling_down", "cooling_down", "recovered"])

    res = run_step6(tmp_path, gcs, quiet=180, poll=15, fake_clock=True,
                    after="archive_active_incidents || exit 1")

    assert res.returncode == 0, out(res)
    assert done_at(res) == 180
    lines = res.stdout.splitlines()
    assert sum("not terminal yet" in line for line in lines) == 2
    assert "quiet period 15s / 180s | 1 incident group(s) not terminal yet" in res.stdout
    assert "Starting the 180s quiet period" in res.stdout
    assert "Quiet period complete (180s >= 180s with zero open ATA alerts) and all incident groups terminal" in res.stdout
    assert "status: recovered" in (root / BUCKET / "archive" / "incident-a.md").read_text()
    assert active_names(root) == []


def test_terminal_after_the_quiet_period_ends_archives_when_terminal(tmp_path, gcs):
    """Alerts close at t=30; the group is recovered 150s later. Archive at the close + 150s,
    not at the close + 60s quiet period + 150s."""
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN),
        sequence=["cooling_down"] * 10 + ["recovered"])

    res = run_step6(tmp_path, gcs, quiet=60, poll=15, alerts=[1, 1, 0], fake_clock=True,
                    after="archive_active_incidents || exit 1")

    assert res.returncode == 0, out(res)
    assert done_at(res) == 30 + 150
    assert "quiet period complete | 1 incident group(s) not terminal yet" in res.stdout
    assert "Zero open ATA alerts and all incident groups terminal" not in res.stdout
    assert "and all incident groups terminal. Proceeding." in res.stdout
    assert "status: recovered" in (root / BUCKET / "archive" / "incident-a.md").read_text()
    assert active_names(root) == []


def test_reopened_alert_restarts_the_quiet_clock(tmp_path, gcs):
    """Close at t=0, reopen at t=30, close again at t=45: archive at 45 + 60s."""
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.RECOVERED))

    res = run_step6(tmp_path, gcs, quiet=60, poll=15, alerts=[0, 0, 1, 0], fake_clock=True)

    assert res.returncode == 0, out(res)
    assert done_at(res) == 45 + 60
    assert res.stdout.count("Starting the 60s quiet period") == 2
    assert "Still open: 1 alert(s) [ata-inventory-5xx (a1)]" in res.stdout


def test_group_regression_does_not_restart_the_quiet_clock(tmp_path, gcs):
    """Only an alert reopening restarts the quiet period; a group that goes back to
    cooling_down only delays the archive until it is terminal again."""
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.RECOVERED),
        sequence=["recovered", "cooling_down", "recovered"])

    res = run_step6(tmp_path, gcs, quiet=60, poll=15, fake_clock=True)

    assert res.returncode == 0, out(res)
    assert done_at(res) == 60
    assert res.stdout.count("Starting the 60s quiet period") == 1
    assert sum("not terminal yet" in line for line in res.stdout.splitlines()) == 1


def test_timeout_while_groups_are_active_fails_before_the_quiet_period_ends(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN))

    res = run_step6(tmp_path, gcs, quiet=180, poll=15, timeout=120, fake_clock=True,
                    after="archive_active_incidents || exit 1")

    assert res.returncode == 1, out(res)
    assert "STEP6_DONE" not in res.stdout
    assert "Timed out after 120s (120s elapsed)" in res.stderr
    assert "  - incident-a.md (cooling_down)" in res.stderr
    assert "NOT archived" in res.stderr
    assert calls(tmp_path, "mv") == []
    assert active_names(root) == ["incident-a.md"]


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


# --- recovered counts only once the recovery note is posted (review R1) --------------
# The agent writes status 'recovered' (claim) BEFORE it posts the recovery note and sets
# recovery_note_posted_at only after the note posted. Archiving in between let the agent's
# post-note write recreate an 'open' stub file in the active folder.


def test_recovered_without_note_posted_stays_active_and_is_listed_on_timeout(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.RECOVERED, note_posted_at=None))
    put(root, "incident-b.md", okf("b", IncidentGroupStatus.RECOVERED))

    res = run_step6(tmp_path, gcs, timeout=0, after="archive_active_incidents || exit 1")

    assert res.returncode == 1, out(res)
    assert "1 incident group(s) are not in a terminal state" in res.stderr
    assert "recovered only once the recovery note is posted" in res.stderr
    assert "  - incident-a.md (recovered, recovery note not posted yet)" in res.stderr
    assert "incident-b.md" not in res.stderr
    assert calls(tmp_path, "mv") == []
    assert active_names(root) == ["incident-a.md", "incident-b.md"]


def test_recovered_with_note_posted_is_archived(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.RECOVERED))

    res = run_step6(tmp_path, gcs, timeout=0, after="archive_active_incidents || exit 1")

    assert res.returncode == 0, out(res)
    assert f"recovery_note_posted_at: '{NOTE_POSTED_AT}'" in (root / BUCKET / "archive" / "incident-a.md").read_text()
    assert active_names(root) == []


def test_waits_through_the_claim_to_note_window_before_archiving(tmp_path, gcs):
    """cooling_down -> recovered (claimed, note pending) x2 -> recovered with the note."""
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.COOLING_DOWN),
        sequence=["cooling_down", "recovered-claimed", "recovered-claimed", "recovered"])

    res = run_step6(tmp_path, gcs, after="archive_active_incidents || exit 1")

    assert res.returncode == 0, out(res)
    progress = [line for line in res.stdout.splitlines() if "not terminal yet" in line]
    assert len(progress) == 3
    assert "[incident-a.md (cooling_down)]" in progress[0]
    assert all("[incident-a.md (recovered, recovery note not posted yet)]" in line for line in progress[1:])
    archived = (root / BUCKET / "archive" / "incident-a.md").read_text()
    assert "status: recovered" in archived
    assert f"recovery_note_posted_at: '{NOTE_POSTED_AT}'" in archived
    assert active_names(root) == []


def group_state(tmp_path, text):
    probe = tmp_path / "state.sh"
    probe.write_text(f'source "{RESET_SCRIPT}"\ngroup_state < "$OKF_DOC"\n')
    data = tmp_path / "doc.md"
    data.write_bytes(text.encode())
    res = subprocess.run([TEST_BASH, str(probe)], capture_output=True, text=True, timeout=30,
                         check=False, env={**os.environ, "MOCK_MODE": "true", "OKF_DOC": str(data)})
    assert res.returncode == 0, out(res)
    return res.stdout.strip()


PENDING = "recovered, recovery note not posted yet"


@pytest.mark.parametrize("note_line, expected", [
    ("recovery_note_posted_at: '2026-10-27T10:03:14Z'", "terminal"),
    ('recovery_note_posted_at: "2026-10-27T10:03:14Z"', "terminal"),
    ("recovery_note_posted_at: 2026-10-27T10:03:14Z\r", "terminal"),
    ("recovery_note_posted_at: null", PENDING),
    ("recovery_note_posted_at: ~", PENDING),
    ("recovery_note_posted_at:", PENDING),
    ("recovery_note_posted_at: ''", PENDING),
    ("other: x", PENDING),
    # A nested key of the same name is not the group's field.
    ("correlation:\n  recovery_note_posted_at: '2026-10-27T10:03:14Z'", PENDING),
])
def test_group_state_recovered_needs_recovery_note_posted_at(tmp_path, note_line, expected):
    text = f"---\ngroup_id: g\nstatus: recovered\n{note_line}\n---\nrecovery_note_posted_at: '2026'\n"
    assert group_state(tmp_path, text) == expected


@pytest.mark.parametrize("status, expected", [
    ("resolved", "terminal"), ("archived", "terminal"), ("degraded", "terminal"),
    ("cooling_down", "cooling_down"), ("open", "open"),
])
def test_group_state_other_statuses_ignore_recovery_note_posted_at(tmp_path, status, expected):
    assert group_state(tmp_path, f"---\ngroup_id: g\nstatus: {status}\nrecovery_note_posted_at: null\n---\n") == expected


# --- --ignore-group: targeted escape for a group that will never become terminal (C2) --


@pytest.mark.parametrize("how", ["file", "id", "env"])
def test_ignore_group_skips_only_that_group_and_keeps_the_other_waits(tmp_path, gcs, how):
    """A stuck 'investigating' group is ignored; the alert wait, the 60s quiet period and
    the wait on the other (cooling) group still run, and the archive warns loudly."""
    _, root = gcs
    put(root, "incident-stuck.md", okf("stuck", IncidentGroupStatus.INVESTIGATING))
    put(root, "incident-b.md", okf("b", IncidentGroupStatus.COOLING_DOWN),
        sequence=["cooling_down"] * 6 + ["recovered"])
    extra, pre = {}, ""
    if how == "env":
        extra["ATA_RESET_IGNORE_GROUPS"] = "incident-other.md,incident-stuck.md"
    else:
        pre = 'IGNORE_GROUPS="%s"\n' % ("incident-stuck.md" if how == "file" else "stuck")

    res = run_step6(tmp_path, gcs, quiet=60, poll=15, alerts=[1, 0], fake_clock=True,
                    after="archive_active_incidents || exit 1", pre=pre, **extra)

    assert res.returncode == 0, out(res)
    # The alert wait ran (1 open at t=0), then the cooling group was waited on until it
    # recovered at the 7th group read (t=15 + 6*15 = 105), past the quiet end (t=75).
    assert "Still open: 1 alert(s)" in res.stdout
    assert done_at(res) == 15 + 6 * 15
    progress = [line for line in res.stdout.splitlines() if "not terminal yet" in line]
    assert progress and all("[incident-b.md (cooling_down)]" in line for line in progress)
    assert "incident-stuck.md" not in "".join(progress)
    assert "[--ignore-group] RISKY" in res.stdout
    assert "[--ignore-group] Archiving 1 incident group(s) that are NOT terminal: incident-stuck.md (investigating)" in res.stdout
    assert active_names(root) == []
    assert "status: investigating" in (root / BUCKET / "archive" / "incident-stuck.md").read_text()


def test_ignore_group_does_not_ignore_other_groups_on_timeout(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-stuck.md", okf("stuck", IncidentGroupStatus.INVESTIGATING))
    put(root, "incident-b.md", okf("b", IncidentGroupStatus.COOLING_DOWN))

    res = run_step6(tmp_path, gcs, timeout=0, pre='IGNORE_GROUPS="stuck"\n',
                    after="archive_active_incidents || exit 1")

    assert res.returncode == 1, out(res)
    assert "  - incident-b.md (cooling_down)" in res.stderr
    assert "incident-stuck.md" not in res.stderr
    assert calls(tmp_path, "mv") == []


def test_ignored_group_that_is_terminal_is_archived_without_the_not_terminal_warning(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.RECOVERED))

    res = run_step6(tmp_path, gcs, pre='IGNORE_GROUPS="a"\n', after="archive_active_incidents || exit 1")

    assert res.returncode == 0, out(res)
    assert "[--ignore-group] RISKY" in res.stdout
    assert "NOT terminal" not in res.stdout
    assert active_names(root) == []


def test_timeout_names_ignore_group_as_the_targeted_escape(tmp_path, gcs):
    _, root = gcs
    put(root, "incident-a.md", okf("a", IncidentGroupStatus.INVESTIGATING))

    res = run_step6(tmp_path, gcs, timeout=0)

    assert res.returncode == 1, out(res)
    assert "--ignore-group <file>" in res.stderr
    assert "--no-wait" in res.stderr


def test_ignore_group_cli_flag_is_repeatable():
    probe = (f'set -- --mock --ignore-group incident-a.md --ignore-group=b\n'
             f'source "{RESET_SCRIPT}"\necho "IG=${{IGNORE_GROUPS}}"\n')
    res = subprocess.run([TEST_BASH, "-c", probe], capture_output=True, text=True, timeout=30, check=False,
                         env={k: v for k, v in os.environ.items() if k != "ATA_RESET_IGNORE_GROUPS"})
    assert res.returncode == 0, out(res)
    assert "IG=incident-a.md b" in res.stdout


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
    assert re.search(r"--ignore-group <file\|id> +Leave one incident group out of the terminal-state wait only", res.stdout)
    assert "Risky: the group is archived as it is" in res.stdout
