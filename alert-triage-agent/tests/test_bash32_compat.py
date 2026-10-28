"""macOS bash 3.2 compatibility of scripts/ and the loud missing-Apigee-key error.

macOS ships /bin/bash 3.2. Under `set -u` it rejects "${ARR[@]}" for an empty array
("ARR[@]: unbound variable"), and it has no associative arrays or ${x,,}/${x^^}.

The scripts run under ATA_TEST_BASH (default: `bash` on PATH). A `bash` shim for it is
put first on PATH, so nested `#!/usr/bin/env bash` scripts use the same shell. To test
against a real bash 3.2: ATA_TEST_BASH=/path/to/bash-3.2.57/bash pytest tests/test_bash32_compat.py
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._stubs import write_stub

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
ALL_SCRIPTS = sorted(SCRIPTS_DIR.rglob("*.sh"))
SCENARIOS = ["s1_backend_5xx.sh", "s2_backend_latency.sh", "s3_concurrent_disjoint.sh",
             "s4_db_connection_pool.sh", "s5_bad_deploy.sh"]
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

# Logs every call (one line of args per call) and prints a 200 status for curl -w.
STUB = '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$ATA_TEST_CALLS"\n{body}\n'


@pytest.fixture
def stub_bin(tmp_path):
    """PATH dir with `bash` -> TEST_BASH and logging gcloud/curl stubs (gcloud prints only a
    service URL: no ID token; the scripts have no fallback URL)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to(TEST_BASH)
    gcloud = 'case "$*" in *"value(status.url)"*) echo "https://inv.example" ;; esac'
    for name, body in [("gcloud", gcloud), ("curl", 'echo 200')]:
        stub = bin_dir / name
        write_stub(stub, STUB.format(body=body))
    return bin_dir


def isolated_scripts(tmp_path):
    """Copy of scripts/ under tmp_path/pkg, so no real .ata-api-key is in the lookup path."""
    scripts = tmp_path / "pkg" / "alert-triage-agent" / "scripts"
    shutil.copytree(SCRIPTS_DIR, scripts, ignore=shutil.ignore_patterns("inventory-pin.*.env"))
    return scripts


def script_env(stub_bin, tmp_path, default_stack_dir=False, **extra):
    """default_stack_dir=True: no ATA_STACK_DIR, so the scripts use their default key location
    (only with an isolated_scripts() copy; the checkout's own may hold a deployed stack's key)."""
    drop = ("ATA_API_KEY", "MOCK_MODE", "DRY_RUN") + (("ATA_STACK_DIR",) if default_stack_dir else ())
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(PATH=f"{stub_bin}{os.pathsep}{env['PATH']}", ATA_TEST_CALLS=str(tmp_path / "calls"),
               MOCK_MODE="false", DRY_RUN="false", RUN_PREFLIGHT="false",
               APIGEE_HOST="apigee.example", APIGEE_ORG="test-org", APIGEE_ENV="test-env")
    env.update(extra)
    return env


def run(cmd, cwd, env, timeout=60):
    return subprocess.run([TEST_BASH, *map(str, cmd)], cwd=str(cwd), env=env, capture_output=True,
                          text=True, timeout=timeout, check=False)


def calls(tmp_path):
    log = tmp_path / "calls"
    return log.read_text().splitlines() if log.exists() else []


def test_test_bash_is_available():
    assert TEST_BASH, f"ATA_TEST_BASH={os.environ.get('ATA_TEST_BASH')!r} not found"


# --- Static checks over every script --------------------------------------------

BARE_ARRAY = re.compile(r'(?<!\[@\]\+)"\$\{[A-Za-z_][A-Za-z0-9_]*\[@\]\}"')
BASH4_ONLY = re.compile(
    r"declare -A|local -A|typeset -A|\bmapfile\b|\breadarray\b|\bcoproc\b|globstar|declare -n|local -n"
    r"|\$\{[^}]*(,,|\^\^)\}|\$\{[A-Za-z0-9_]+[,^]\}|\|&|&>>|\[\[ -v "
)


def code_lines(path):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: str(p.relative_to(SCRIPTS_DIR)))
def test_no_bare_array_expansion(script):
    """Every "${ARR[@]}" must be written ${ARR[@]+"${ARR[@]}"} (empty-array safe under set -u)."""
    bad = [f"{n}: {line.strip()}" for n, line in code_lines(script) if BARE_ARRAY.search(line)]
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: str(p.relative_to(SCRIPTS_DIR)))
def test_no_bash4_only_features(script):
    bad = [f"{n}: {line.strip()}" for n, line in code_lines(script) if BASH4_ONLY.search(line)]
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: str(p.relative_to(SCRIPTS_DIR)))
def test_scripts_parse(script):
    res = subprocess.run([TEST_BASH, "-n", str(script)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


# --- Missing or empty Apigee key: fail before any fault is injected ----------------


@pytest.mark.parametrize("sc", SCENARIOS)
def test_scenario_fails_loudly_without_api_key(sc, tmp_path, stub_bin):
    scripts = isolated_scripts(tmp_path)
    res = run([scripts / "scenarios" / sc, "--hold", "1s"], tmp_path, script_env(stub_bin, tmp_path, default_stack_dir=True))

    assert res.returncode != 0, res.stdout + res.stderr
    for text in ["Apigee consumer API key not found for stack 'ata'", "ATA_API_KEY",
                 f"{tmp_path}/pkg/alert-triage-agent/.ata-api-key", "deploy-apigee-proxies.sh",
                 "organizations/test-org/developers/ata-dev@example.com/apps/ata-demo-app", "DEMO.md"]:
        assert text in res.stderr, text
    assert "unbound variable" not in res.stderr
    assert calls(tmp_path) == [], "no gcloud/curl call (no fault injected) before the key check"


@pytest.mark.parametrize("sc", SCENARIOS)
def test_scenario_fails_loudly_on_empty_key_file(sc, tmp_path, stub_bin):
    """An empty .<prefix>-api-key (e.g. a failed DEMO.md fetch) is an error, not an empty key."""
    scripts = isolated_scripts(tmp_path)
    (tmp_path / "pkg" / "alert-triage-agent" / ".ata-api-key").write_text("\n")
    res = run([scripts / "scenarios" / sc, "--hold", "1s"], tmp_path, script_env(stub_bin, tmp_path, default_stack_dir=True))

    assert res.returncode != 0, res.stdout + res.stderr
    assert f"{tmp_path}/pkg/alert-triage-agent/.ata-api-key is empty" in res.stderr
    assert "developers/ata-dev@example.com/apps/ata-demo-app" in res.stderr
    assert calls(tmp_path) == []


def test_stack_key_file_is_used_and_trimmed(tmp_path, stub_bin):
    scripts = isolated_scripts(tmp_path)
    (tmp_path / "pkg" / "alert-triage-agent" / ".ata-api-key").write_text("  pkg-key \n")
    (tmp_path / ".ata-api-key").write_text("cwd-key\n")  # not a key location (stack isolation)
    probe = tmp_path / "probe.sh"
    probe.write_text(f'set -euo pipefail\nSCRIPT_DIR="{scripts}/scenarios"\n'
                     f'source "{scripts}/scenarios/common.sh"\nresolve_api_key\n')
    res = run([probe], tmp_path, script_env(stub_bin, tmp_path, default_stack_dir=True))
    assert (res.returncode, res.stdout) == (0, "pkg-key\n"), res.stderr


@pytest.mark.parametrize("prefix", ["atat", "data"])
def test_key_of_another_stack_or_directory_is_never_used(prefix, tmp_path, stub_bin):
    """Only <alert-triage-agent>/.${NAME_PREFIX}-api-key: never .ata-api-key, never the cwd."""
    scripts = isolated_scripts(tmp_path)
    (tmp_path / "pkg" / "alert-triage-agent" / ".ata-api-key").write_text("ata-key\n")
    (tmp_path / f".{prefix}-api-key").write_text("cwd-key\n")
    probe = tmp_path / "probe.sh"
    probe.write_text(f'set -euo pipefail\nSCRIPT_DIR="{scripts}/scenarios"\n'
                     f'source "{scripts}/scenarios/common.sh"\nresolve_api_key\n')
    pin = tmp_path / f"inventory-pin.{prefix}.env"
    pin.write_text(f"PIN_INVENTORY_REVISION={prefix}-inventory-api-00001-abc\n"
                   f"PIN_INVENTORY_DIGEST=sha256:{'ab' * 32}\n")
    res = run([probe], tmp_path, script_env(stub_bin, tmp_path, default_stack_dir=True, NAME_PREFIX=prefix,
                                            ATA_INVENTORY_PIN_FILE=str(pin)))
    assert res.returncode != 0 and res.stdout == "", res.stdout + res.stderr
    assert f"{tmp_path}/pkg/alert-triage-agent/.{prefix}-api-key" in res.stderr


@pytest.mark.parametrize("mode", ["--mock", "--dry-run"])
@pytest.mark.parametrize("sc", SCENARIOS)
def test_scenario_mock_and_dry_run_need_no_key(sc, mode, tmp_path, stub_bin):
    # Real scripts/ (S3 reads demo/apigee); a developer's key file there is harmless.
    res = run([SCRIPTS_DIR / "scenarios" / sc, mode, "--hold", "0s"], tmp_path, script_env(stub_bin, tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "NEXT STEPS" in res.stdout


@pytest.mark.parametrize("mode", ["--mock", "--dry-run"])
@pytest.mark.parametrize("sc", SCENARIOS)
def test_scenario_mock_and_dry_run_skip_the_key_check(sc, mode, tmp_path, stub_bin):
    """Mock/dry-run never call Apigee: no key, or an empty key file, is not an error there."""
    scripts = isolated_scripts(tmp_path)
    shutil.copytree(SCRIPTS_DIR.parent / "demo" / "apigee", scripts.parent / "demo" / "apigee")  # S3 reads it
    (tmp_path / "pkg" / "alert-triage-agent" / ".ata-api-key").write_text("\n")
    res = run([scripts / "scenarios" / sc, mode, "--hold", "0s"], tmp_path, script_env(stub_bin, tmp_path, default_stack_dir=True))

    assert res.returncode == 0, res.stdout + res.stderr
    assert "NEXT STEPS" in res.stdout
    assert "Apigee consumer API key" not in res.stdout + res.stderr


@pytest.mark.parametrize("mode", ["MOCK_MODE", "DRY_RUN"])
def test_resolve_api_key_prints_nothing_in_mock_and_dry_run(mode, tmp_path, stub_bin):
    """No placeholder key: mock/dry-run resolve to an empty key without reading any key source."""
    scripts = isolated_scripts(tmp_path)
    (tmp_path / "pkg" / "alert-triage-agent" / ".ata-api-key").write_text("\n")
    probe = tmp_path / "probe.sh"
    probe.write_text(f'set -euo pipefail\nSCRIPT_DIR="{scripts}/scenarios"\n'
                     f'source "{scripts}/scenarios/common.sh"\nresolve_api_key\necho RESOLVED\n')
    res = run([probe], tmp_path, script_env(stub_bin, tmp_path, default_stack_dir=True, **{mode: "true"}))
    assert (res.returncode, res.stdout, res.stderr) == (0, "RESOLVED\n", "")


# --- Live code paths with empty arrays under set -u ------------------------------


@pytest.mark.parametrize("sc", ["s1_backend_5xx.sh", "s2_backend_latency.sh"])
def test_live_scenario_runs_with_empty_auth_header(sc, tmp_path, stub_bin):
    """gcloud yields no ID token, so AUTH_HEADER is empty: the curl calls must still run."""
    scripts = isolated_scripts(tmp_path)
    res = run([scripts / "scenarios" / sc, "--hold", "1s"], tmp_path,
              script_env(stub_bin, tmp_path, default_stack_dir=True, ATA_API_KEY="test-key"))

    assert res.returncode == 0, res.stdout + res.stderr
    assert "unbound variable" not in res.stderr
    curl_calls = [c for c in calls(tmp_path) if c.split()[0].endswith("/curl")]
    assert any("/admin/fault/inject" in c for c in curl_calls)
    assert any("x-api-key: test-key" in c and "/ata/inventory/v1/items" in c for c in curl_calls)
    # No token: no Authorization header at all (not the warning text as a Bearer token).
    assert not any("Authorization" in c for c in curl_calls)
    assert "Could not resolve identity token" in res.stderr


def test_check_result_case_conversion(tmp_path, stub_bin):
    """check-result.sh lowercases --scenario and prints it uppercased (was ${2,,} / ${x^^})."""
    env = script_env(stub_bin, tmp_path)
    for args in (["--scenario", "S1"], ["--scenario=S1"]):
        res = run([SCRIPTS_DIR / "scenarios" / "check-result.sh", "--mock", *args], tmp_path, env)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "Target Scenario: S1" in res.stdout
        assert "[S1: Backend 5xx Error Spike]" in res.stdout
        assert "[MATCH]" in res.stdout


# --- run-all-scenarios.sh without associative arrays ------------------------------

STUB_SCENARIO = '#!/usr/bin/env bash\necho "{name}" >> "$ATA_TEST_RAN"\nexit {rc}\n'


def make_driver(tmp_path, failing=()):
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS_DIR, scripts, ignore=shutil.ignore_patterns("inventory-pin.*.env"))
    for script in (scripts / "scenarios").glob("s[1-5]_*.sh"):
        name = script.name.split("_")[0]
        write_stub(script, STUB_SCENARIO.format(name=name, rc=1 if name in failing else 0))
    (scripts / "reset-scenarios.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    return scripts / "run-all-scenarios.sh"


def summary_rows(stdout):
    summary = stdout.split("SCENARIO EXECUTION SUMMARY", 1)[1]
    return [line.rstrip() for line in summary.splitlines() if re.match(r"S\d ", line)]


def run_driver(tmp_path, stub_bin, *args, failing=()):
    driver = make_driver(tmp_path, failing)
    env = script_env(stub_bin, tmp_path, SKIP_PREFLIGHT="true", MOCK_MODE="true",
                     ATA_TEST_RAN=str(tmp_path / "ran"))
    return run([driver, *args], tmp_path, env)


def test_run_all_priority_names_statuses_and_durations(tmp_path, stub_bin):
    res = run_driver(tmp_path, stub_bin, "--priority", failing=("s3",))

    assert res.returncode == 1, res.stdout + res.stderr
    assert (tmp_path / "ran").read_text().split() == ["s4", "s1", "s3", "s5"]
    assert "Executing Scenario: S4 - Database Connection Pool Exhaustion (Cloud SQL max_conn)" in res.stdout
    assert "Scenario S3 failed with exit code 1" in res.stdout
    rows = summary_rows(res.stdout)
    assert [r.split()[0] for r in rows] == ["S4", "S1", "S3", "S5"]
    assert [r.split()[-2] for r in rows] == ["PASSED", "PASSED", "FAILED", "PASSED"]
    assert all(re.search(r"\d+s$", r) for r in rows)
    assert "Backend 5xx Error Spike (Cloud Run ata-inve " in rows[1]
    assert "1 scenario(s) failed" in res.stdout


def test_run_all_single_uppercase_id(tmp_path, stub_bin):
    res = run_driver(tmp_path, stub_bin, "--scenario", "S2")

    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "ran").read_text().split() == ["s2"]
    assert "Executing Scenario: S2 - Backend Latency Degradation (>5s timeout)" in res.stdout
    assert [r.split()[-2] for r in summary_rows(res.stdout)] == ["PASSED"]


def test_run_all_unknown_scenario(tmp_path, stub_bin):
    res = run_driver(tmp_path, stub_bin, "--scenario", "s9")

    assert res.returncode == 1, res.stdout + res.stderr
    rows = summary_rows(res.stdout)
    assert len(rows) == 1 and rows[0].split()[:2] == ["S9", "Unknown"] and rows[0].split()[-2:] == ["NOT_FOUND", "0s"]
