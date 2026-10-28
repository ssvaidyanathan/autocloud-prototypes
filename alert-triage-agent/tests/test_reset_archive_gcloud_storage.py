"""reset-scenarios.sh step 7/8 (and the other incident-bucket readers) use gcloud storage
and fail loudly.

Step 7 used `gsutil ... 2>/dev/null || true` and logged success either way, so a missing
gsutil or rejected credentials left resolved incidents in knowledge/incident-manager/
while the reset reported success. Step 8 "verified" 0 active groups from a variable
nothing set.

A fake `gcloud` (FAKE_GCLOUD) serves `gcloud storage ls/mv/rm/cat` from a local
directory, with gcloud's own "matched no objects" errors, and fails the commands listed
in ATA_FAKE_GCS_FAIL the way rejected credentials do. reset-scenarios.sh is sourced
(which defines its functions without running the reset). Scripts run under
ATA_TEST_BASH, as in test_bash32_compat.py.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
RESET_SCRIPT = SCRIPTS_DIR / "reset-scenarios.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
BUCKET = "proj-ata-incidents"
AUTH_ERROR = "HTTPError 401: Invalid Credentials"

FAKE_GCLOUD = '''#!{python} -I
import fnmatch, os, shutil, sys

args = sys.argv[1:]
with open(os.environ["ATA_TEST_CALLS"], "a") as log:
    log.write("gcloud " + " ".join(args) + "\\n")
if args[:1] != ["storage"]:
    sys.exit(0)  # other gcloud calls print nothing
cmd, urls = args[1], args[2:]
root = os.environ["ATA_FAKE_GCS_ROOT"]

if cmd == "objects" and urls[:1] == ["describe"]:
    # The object's update time is the local file's mtime, in gcloud's format.
    import json, time
    path = os.path.join(root, urls[1][len("gs://"):])
    if not os.path.isfile(path):
        sys.stderr.write("ERROR: (gcloud.storage.objects.describe) %s not found: 404.\\n" % urls[1])
        sys.exit(1)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(os.path.getmtime(path)))
    print(json.dumps({{"name": os.path.basename(path), "update_time": stamp, "creation_time": stamp}}))
    sys.exit(0)

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
    if cmd == "ls":
        sys.stderr.write("ERROR: (gcloud.storage.ls) One or more URLs matched no objects.\\n")
    else:
        sys.stderr.write("ERROR: (gcloud.storage.%s) The following URLs matched no objects or files:\\n-%s\\n"
                         % (cmd, url))
    sys.exit(1)

if cmd == "mv":
    srcs, dst = urls[:-1], local(urls[-1])
    found = [p for u in srcs for p in matches(u)] or no_match(srcs[0])
    os.makedirs(dst, exist_ok=True)
    for p in found:
        sys.stderr.write("Copying %s\\n" % p)
        shutil.move(p, os.path.join(dst, os.path.basename(p)))
    sys.exit(0)
found = matches(urls[0]) or no_match(urls[0])
for p in found:
    if cmd == "ls":
        print("gs://" + os.path.relpath(p, root))
    elif cmd == "rm":
        sys.stderr.write("Removing %s\\n" % p)
        os.remove(p)
    elif cmd == "cat":
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


def put(root, name, text="x\n"):
    path = root / BUCKET / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def names(root, prefix):
    folder = root / BUCKET / prefix
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def env_for(tmp_path, gcs, fail="", **extra):
    bin_dir, root = gcs
    env = {k: v for k, v in os.environ.items() if k not in ("ATA_API_KEY", "INCIDENTS_BUCKET")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", ATA_TEST_CALLS=str(tmp_path / "calls"),
               ATA_FAKE_GCS_ROOT=str(root), ATA_FAKE_GCS_FAIL=fail,
               MOCK_MODE="false", DRY_RUN="false", PROJECT_ID="proj")
    env.update(extra)
    return env


def run_reset_functions(tmp_path, gcs, *functions, fail=""):
    """Source reset-scenarios.sh and run `f || exit 1` for each function, like the reset does."""
    probe = tmp_path / "probe.sh"
    body = "".join(f"{f} || exit 1\n" for f in functions)
    probe.write_text(f'set -euo pipefail\nsource "{RESET_SCRIPT}"\n{body}echo PROBE_DONE\n')
    return subprocess.run([TEST_BASH, str(probe)], cwd=str(tmp_path), env=env_for(tmp_path, gcs, fail),
                          capture_output=True, text=True, timeout=60, check=False)


def gcloud_calls(tmp_path):
    log = tmp_path / "calls"
    return log.read_text().splitlines() if log.exists() else []


# --- Step 7: archive -------------------------------------------------------------

STEP7 = ("archive_active_incidents", "clean_root_copies", "remove_verify_report")
STEP7_AND_8 = STEP7 + ("verify_no_active_incident_groups",)


def test_archive_moves_active_incidents_and_verifies_zero_groups(tmp_path, gcs):
    _, root = gcs
    for inc in ("INC0010316", "INC0010317"):
        put(root, f"knowledge/incident-manager/{inc}.md")
    put(root, "knowledge/incident-manager/notes.txt")
    put(root, "archive/INC0010001.md")

    res = run_reset_functions(tmp_path, gcs, *STEP7_AND_8)

    assert res.returncode == 0, res.stdout + res.stderr
    assert names(root, "knowledge/incident-manager") == ["notes.txt"]
    assert names(root, "archive") == ["INC0010001.md", "INC0010316.md", "INC0010317.md"]
    assert "Moving 2 active incident file(s)" in res.stdout
    assert "Active incident files moved from knowledge/incident-manager/ to archive/." in res.stdout
    assert "Verified 0 active incident groups" in res.stdout
    assert f"gcloud storage mv gs://{BUCKET}/knowledge/incident-manager/*.md gs://{BUCKET}/archive/" \
        in gcloud_calls(tmp_path)
    assert res.stderr == ""


def test_nothing_to_archive_is_success(tmp_path, gcs):
    """An empty bucket: every ls/rm matches nothing, which is not an error."""
    _, root = gcs
    res = run_reset_functions(tmp_path, gcs, *STEP7_AND_8)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "PROBE_DONE" in res.stdout
    assert "No active incident files found in knowledge/incident-manager/." in res.stdout
    assert "No leftover root incident copies" in res.stdout
    assert "Verified 0 active incident groups" in res.stdout
    assert "moved" not in res.stdout
    assert not any(" mv " in c for c in gcloud_calls(tmp_path))
    assert res.stderr == ""


def test_root_copies_and_verify_report_are_removed(tmp_path, gcs):
    _, root = gcs
    put(root, "INC0010318.md")
    put(root, "INC0010319.md")
    put(root, "reports/verify_highlighting_001.html")
    put(root, "reports/keep.html")

    res = run_reset_functions(tmp_path, gcs, *STEP7)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Deleting 2 leftover root incident copies" in res.stdout
    assert "Leftover root copies removed." in res.stdout
    assert not list((root / BUCKET).glob("*.md"))
    assert names(root, "reports") == ["keep.html"]


@pytest.mark.parametrize("fail, archived, message", [
    ("ls", False, "could not list gs://{b}/knowledge/incident-manager/*.md; incident files were NOT archived"),
    ("mv", False, "gcloud storage mv gs://{b}/knowledge/incident-manager/*.md gs://{b}/archive/ failed"),
    ("rm", True, "gcloud storage rm gs://{b}/*.md failed; leftover root copies were NOT removed"),
])
def test_archive_failure_is_fatal_and_never_reports_success(tmp_path, gcs, fail, archived, message):
    _, root = gcs
    put(root, "knowledge/incident-manager/INC0010316.md")
    put(root, "INC0010318.md")

    res = run_reset_functions(tmp_path, gcs, *STEP7_AND_8, fail=fail)

    assert res.returncode == 1, res.stdout + res.stderr
    assert "PROBE_DONE" not in res.stdout
    assert message.format(b=BUCKET) in res.stderr
    assert AUTH_ERROR in res.stderr, "gcloud's own error is shown"
    assert "gcloud auth list" in res.stderr and "roles/storage.objectAdmin" in res.stderr
    # The only success line is the archive itself, and only when the mv really ran.
    assert res.stdout.count("[SUCCESS]") == (1 if archived else 0)
    assert ("moved from knowledge/incident-manager/" in res.stdout) is archived
    assert names(root, "archive") == (["INC0010316.md"] if archived else [])
    assert "Verified 0 active incident groups" not in res.stdout


def test_missing_gcloud_is_fatal(tmp_path, gcs):
    bin_dir, _ = gcs
    probe = tmp_path / "probe.sh"
    probe.write_text(f'set -euo pipefail\nsource "{RESET_SCRIPT}"\n'
                     "archive_active_incidents || exit 1\necho PROBE_DONE\n")
    # PATH holds only bash and coreutils: no gcloud anywhere.
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "bash").symlink_to(TEST_BASH)
    for tool in ("dirname", "mktemp", "grep", "cat", "rm", "tr", "date"):
        (tools / tool).symlink_to(shutil.which(tool))
    env = env_for(tmp_path, gcs, PATH=str(tools))
    res = subprocess.run([TEST_BASH, str(probe)], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=60, check=False)

    assert res.returncode == 1, res.stdout + res.stderr
    assert "incident files were NOT archived" in res.stderr
    assert "gcloud" in res.stderr and "not found" in res.stderr
    assert "PROBE_DONE" not in res.stdout


# --- Step 8: 0 active incident groups ----------------------------------------------


def test_verify_fails_when_incidents_remain(tmp_path, gcs):
    _, root = gcs
    put(root, "knowledge/incident-manager/INC0010316.md")
    put(root, "knowledge/incident-manager/INC0010317.md")

    res = run_reset_functions(tmp_path, gcs, "verify_no_active_incident_groups")

    assert res.returncode == 1, res.stdout + res.stderr
    assert "2 incident file(s) still active after archiving" in res.stderr
    assert f"gs://{BUCKET}/knowledge/incident-manager/INC0010316.md" in res.stderr
    assert "Verified 0 active incident groups" not in res.stdout


def test_verify_fails_when_listing_fails(tmp_path, gcs):
    res = run_reset_functions(tmp_path, gcs, "verify_no_active_incident_groups", fail="ls")

    assert res.returncode == 1, res.stdout + res.stderr
    assert "could not list" in res.stderr and "to verify 0 active incident groups" in res.stderr
    assert AUTH_ERROR in res.stderr
    assert "Verified 0 active incident groups" not in res.stdout


# --- The reset wires them in, and nothing uses gsutil ------------------------------


def test_reset_steps_7_and_8_exit_on_failure():
    text = RESET_SCRIPT.read_text()
    main = text.split('if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then', 1)[1]
    step7 = main.split("# 7. Archive", 1)[1].split("# 8. ", 1)[0]
    step8 = main.split("# 8. ", 1)[1].split("# 9. ", 1)[0]
    for fn in STEP7:
        assert f"    {fn} || exit 1\n" in step7
    assert "    verify_no_active_incident_groups || exit 1\n" in step8
    assert "|| true" not in step7
    assert 'echo "200"' not in step8, "a failed health probe must not read as HTTP 200"
    assert "BRIDGE_RESET_RESP" not in text


@pytest.mark.parametrize("script", sorted(SCRIPTS_DIR.rglob("*.sh")), ids=lambda p: str(p.relative_to(SCRIPTS_DIR)))
def test_no_gsutil_in_scripts(script):
    bad = [f"{n}: {line.strip()}" for n, line in enumerate(script.read_text().splitlines(), 1)
           if re.search(r"\bgsutil\b", line) and not line.lstrip().startswith("#")]
    assert not bad, "\n".join(bad)


# --- verify-scenarios.sh and check-result.sh read the bucket the same way ------------


def run_script(tmp_path, gcs, cmd, fail=""):
    return subprocess.run([TEST_BASH, *map(str, cmd)], cwd=str(tmp_path), env=env_for(tmp_path, gcs, fail),
                          capture_output=True, text=True, timeout=60, check=False)


INCIDENT_MD = """---
type: incident
group_id: grp-1
timestamp: "2026-10-27T01:00:00Z"
origin: ata-inventory-api
servicenow_number: INC0010316
status: RESOLVED
---
body
"""


def test_check_result_lists_active_and_archived_incidents(tmp_path, gcs):
    _, root = gcs
    put(root, "knowledge/incident-manager/INC0010316.md", INCIDENT_MD)
    put(root, "archive/INC0010317.md", INCIDENT_MD.replace("INC0010316", "INC0010317"))

    res = run_script(tmp_path, gcs, [SCRIPTS_DIR / "scenarios" / "check-result.sh"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Found 2 incident group(s) in GCS" in res.stdout
    assert "INC0010316" in res.stdout and "INC0010317" in res.stdout


def test_check_result_empty_bucket_is_zero_incidents(tmp_path, gcs):
    res = run_script(tmp_path, gcs, [SCRIPTS_DIR / "scenarios" / "check-result.sh"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Found 0 incident group(s) in GCS" in res.stdout


def test_check_result_listing_failure_is_fatal(tmp_path, gcs):
    _, root = gcs
    put(root, "knowledge/incident-manager/INC0010316.md", INCIDENT_MD)

    res = run_script(tmp_path, gcs, [SCRIPTS_DIR / "scenarios" / "check-result.sh"], fail="ls")

    assert res.returncode != 0, res.stdout + res.stderr
    assert f"gcloud storage ls gs://{BUCKET}/knowledge/incident-manager/*.md failed" in res.stderr
    assert AUTH_ERROR in res.stderr
    assert "Found 0 incident group(s)" not in res.stdout


@pytest.mark.parametrize("fail, rc, text", [
    ("", 0, "No active incident files currently in storage"),
    ("ls", 1, "could not list gs://proj-ata-incidents/knowledge/incident-manager/"),
])
def test_verify_scenarios_gcs_step(tmp_path, gcs, fail, rc, text):
    script = SCRIPTS_DIR / "verify-scenarios.sh"
    probe = tmp_path / "probe.sh"
    # Run only step 3 of verify-scenarios.sh: its lines from "# 3." up to "# 4.".
    lines = script.read_text().split("# 3. Check GCS OKF Incident Files", 1)[1].split("# 4. ", 1)[0]
    probe.write_text(f'set -euo pipefail\nSCRIPT_DIR="{SCRIPTS_DIR}"\n'
                     f'source "{SCRIPTS_DIR}/scenarios/common.sh"\n{lines}\necho PROBE_DONE\n')

    res = run_script(tmp_path, gcs, [probe], fail=fail)

    assert res.returncode == rc, res.stdout + res.stderr
    assert text in res.stdout + res.stderr


# --- check-result.sh without PyYAML (D9 of the atat proof) ----------------------------
# The operator scripts need only the standard library. Without PyYAML, check-result.sh used
# to fall back silently to a line parser that dropped every list: "Alert Count: 0", no
# labels, no blast radius. Its built-in parser must read what the agent writes.

AGENT_STYLE_MD = """---
type: incident
group_id: stk-inventory-5xx-stk-inventory-api-20261027t202220z
status: recovered
origin: stk-inventory-api
blast_radius:
- stk-inventory-api
- stk-orders-api
priority: P1
servicenow_number: INC0010999
ticket_short_description: '[P1] stk-inventory-api: Elevated HTTP 5xx error rate (2
  alerts)'
timestamp: '2026-10-27T20:22:20Z'
confidence: 0.81
alerts:
- incident_id: 0.aaa
  policy_name: stk-inventory-5xx
  resource_name: stk-inventory-api
  signal: 5xx
  labels: &id001
    component: stk-inventory-api
    severity: p1
  policy_user_labels: *id001
- incident_id: 0.bbb
  policy_name: stk-orders-5xx
  resource_name: stk-orders-api
  signal: 5xx
  labels:
    component: stk-orders-api
    severity: p1
---
body
"""


def test_check_result_without_pyyaml_reads_alerts_labels_and_blast_radius(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-stk.md", AGENT_STYLE_MD)
    # `python3 -` puts the working directory first on sys.path: this hides PyYAML.
    (tmp_path / "yaml.py").write_text("raise ImportError('test: PyYAML not installed')\n")

    res = run_script(tmp_path, gcs, [SCRIPTS_DIR / "scenarios" / "check-result.sh"])

    assert res.returncode == 0, res.stdout + res.stderr
    assert "PyYAML is not installed" in res.stderr  # stated, not silent
    assert "Alert Count:    2" in res.stdout
    assert "* stk-inventory-5xx (resource: stk-inventory-api, signal: 5xx)" in res.stdout
    assert "Labels: component=stk-inventory-api, severity=p1" in res.stdout
    assert "* stk-orders-5xx (resource: stk-orders-api, signal: 5xx)" in res.stdout
    assert "Blast Radius:   stk-inventory-api, stk-orders-api" in res.stdout
    assert "INC0010999" in res.stdout


def _stdlib_parser():
    src = (SCRIPTS_DIR / "scenarios" / "check-result.sh").read_text()
    py = src.split("<< 'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    ns = {"re": re}
    exec(py[py.index("_FM_KEY ="):py.index("_YAML_WARNED = []")], ns)
    return ns["parse_frontmatter_stdlib"]


def test_stdlib_frontmatter_parser_matches_pyyaml_on_safe_dump_output():
    yaml = pytest.importorskip("yaml")
    labels = {"app": "alert-triage-agent", "component": "stk-orders-api", "severity": "p1"}
    doc = {
        "type": "incident", "status": "recovered", "priority": "P1", "confidence": 0.81,
        "open_groups_compared": 0, "degraded": False, "cooling_deadline": None, "empty": [],
        "policy_id": "17930029511410848304",
        "ticket_short_description": "[P1] stk-inventory-api: Elevated HTTP 5xx error rate (2 alerts) " * 2,
        "blast_radius": ["stk-inventory-api", "a long resource name {project_id=p, resource_container=1, "
                         "proxy_name=stk-orders-v1} that wraps"],
        "correlation": {"decision": "new_group", "best_score": 0.0, "rationale": "it's new: no groups"},
        "alerts": [{"policy_name": "stk-orders-5xx", "labels": labels, "policy_user_labels": labels},
                   {"policy_name": "stk-inventory-5xx", "labels": {}, "url": "https://x/y?a=b&c=d"}],
    }
    text = yaml.safe_dump(doc, sort_keys=False)
    assert "&id001" in text and "*id001" in text  # the alias path is exercised
    assert _stdlib_parser()(text) == yaml.safe_load(text) == doc


# --- check-result.sh --since: a file the window cannot place is not counted -------------
# Live QA S1 re-run (INC0010344): an archived stub from 2026-10-22 with timestamp: '' and
# started_at: '' passed the --since filter (no time -> no comparison), so S1 showed 2 groups.

SINCE = "2026-10-27T23:52:48Z"


def incident_md(group_id, timestamp, started_at, number="", origin="ata-inventory-api", status="recovered"):
    return f"""---
type: incident
group_id: {group_id}
project: proj
status: {status}
timestamp: {timestamp}
origin: {origin}
servicenow_number: '{number}'
alerts:
- incident_id: x
  policy: p
  state: open
  started_at: {started_at}
---
body
"""


# The archived file of the live finding, with its fields as they are in the bucket.
LIVE_UNDATED_STUB = incident_md("grp_20261022t234418z_ata-inventory-db", "''", "''",
                                origin="ata-inventory-db", status="mitigation-in-progress")
UNDATED = incident_md("grp-undated", "''", "''", number="INC0000001", status="mitigation-in-progress")
OLD = incident_md("grp-old", "'2026-10-22T23:40:09Z'", "'2026-10-22T23:40:09Z'", number="INC0000002")
IN_WINDOW = incident_md("grp_20261028t000139z_ata-inventory-api", "'2026-10-28T00:01:39Z'",
                        "'2026-10-28T00:01:39Z'", number="INC0010344")


def check_since(tmp_path, gcs, since=SINCE, no_pyyaml=False):
    if no_pyyaml:  # `python3 -` puts the working directory first on sys.path: this hides PyYAML.
        (tmp_path / "yaml.py").write_text("raise ImportError('test: PyYAML not installed')\n")
    return run_script(tmp_path, gcs, [SCRIPTS_DIR / "scenarios" / "check-result.sh", "--since", since,
                                      "--scenario", "s1"])


def test_check_result_since_excludes_undated_archived_file_with_warning(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-undated.md", UNDATED)

    res = check_since(tmp_path, gcs)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Found 0 incident group(s) in GCS" in res.stdout
    assert (f"[WARNING] gs://{BUCKET}/archive/incident-undated.md: undetermined time; "
            "excluded from --since window") in res.stderr
    assert "1 incident file(s) with undetermined time excluded" in res.stdout
    assert "INC0000001" not in res.stdout


def test_check_result_since_excludes_dated_old_file(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-old.md", OLD)

    res = check_since(tmp_path, gcs)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Found 0 incident group(s) in GCS" in res.stdout
    assert "INC0000002" not in res.stdout
    assert "WARNING" not in res.stderr


@pytest.mark.parametrize("prefix", ["knowledge/incident-manager", "archive"])
def test_check_result_since_counts_dated_in_window_file(tmp_path, gcs, prefix):
    _, root = gcs
    put(root, f"{prefix}/incident-s1.md", IN_WINDOW)

    res = check_since(tmp_path, gcs)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Found 1 incident group(s) in GCS" in res.stdout
    assert "INC0010344" in res.stdout
    assert "Timestamp:      2026-10-28T00:01:39Z" in res.stdout


@pytest.mark.parametrize("no_pyyaml", [False, True], ids=["pyyaml", "stdlib-yaml"])
def test_check_result_since_s1_shaped_bucket_counts_one_group(tmp_path, gcs, no_pyyaml):
    if not no_pyyaml:
        pytest.importorskip("yaml")
    _, root = gcs
    put(root, "knowledge/incident-manager/incident-ata-inventory-5xx-ata-inventory-api-20261028t000139z.md",
        IN_WINDOW)
    put(root, "archive/incident-ata-cloudsql-connections-critical-ata-inventory-db-20261022t234418z.md",
        LIVE_UNDATED_STUB)
    put(root, "archive/incident-test-test-20261022t234009z.md", OLD)
    put(root, "archive/incident-undated.md", UNDATED)

    res = check_since(tmp_path, gcs, no_pyyaml=no_pyyaml)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Found 1 incident group(s) in GCS" in res.stdout
    assert "Group Count:      Expected 1 -> [MATCH]" in res.stdout
    # The live stub has no time fields, but its group id carries its creation time (old).
    assert ("incident-ata-cloudsql-connections-critical-ata-inventory-db-20261022t234418z.md: "
            "undetermined time") not in res.stderr
    assert f"[WARNING] gs://{BUCKET}/archive/incident-undated.md: undetermined time" in res.stderr


@pytest.mark.parametrize("age_s, found", [(60, 1), (10 * 86400, 0)])
def test_check_result_since_undated_active_file_uses_gcs_object_time(tmp_path, gcs, age_s, found):
    _, root = gcs
    put(root, "knowledge/incident-manager/incident-undated.md", UNDATED)
    path = root / BUCKET / "knowledge/incident-manager/incident-undated.md"
    mtime = time.time() - age_s
    os.utime(path, (mtime, mtime))
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))

    res = check_since(tmp_path, gcs, since=since)

    assert res.returncode == 0, res.stdout + res.stderr
    assert f"Found {found} incident group(s) in GCS" in res.stdout
    assert "undetermined time" not in res.stderr
    if found:
        assert "taken from GCS object update_time" in res.stderr


def test_check_result_since_ignores_gcs_time_of_archived_file(tmp_path, gcs):
    """An archived object's GCS time is the reset's move: it must not place the file in the window."""
    _, root = gcs
    put(root, "archive/incident-undated.md", UNDATED)  # mtime: now, after --since

    res = check_since(tmp_path, gcs, since="2026-01-01T00:00:00Z")

    assert "Found 0 incident group(s) in GCS" in res.stdout
    assert "undetermined time; excluded from --since window" in res.stderr
    assert not [c for c in gcloud_calls(tmp_path) if "objects describe" in c]


def test_check_result_invalid_since_is_an_error(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-old.md", OLD)

    res = check_since(tmp_path, gcs, since="yesterday")

    assert res.returncode != 0
    assert "--since 'yesterday' is not an ISO-8601 time" in res.stderr


def test_check_result_parse_iso_accepts_gcloud_and_agent_formats():
    src = (SCRIPTS_DIR / "scenarios" / "check-result.sh").read_text()
    py = src.split("<< 'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    from datetime import datetime, timezone
    ns = {"re": re, "datetime": datetime, "timezone": timezone}
    exec(py[py.index("def parse_iso("):py.index("# The agent's group ids")], ns)
    parse_iso = ns["parse_iso"]
    want = datetime(2026, 10, 28, 0, 1, 39, tzinfo=timezone.utc)
    for text in ("2026-10-28T00:01:39Z", "2026-10-28T00:01:39+0000", "2026-10-28T00:01:39+00:00",
                 "2026-10-28T00:01:39", "2026-10-28T02:01:39+02:00"):
        assert parse_iso(text) == want, text
    assert parse_iso("2026-10-28T00:01:39.1234567Z").replace(microsecond=0) == want
    assert parse_iso(datetime(2026, 10, 28, 0, 1, 39)) == want  # PyYAML's unquoted timestamp
    for empty in ("", "''", None, "N/A", "yesterday"):
        assert parse_iso(empty) is None, empty
