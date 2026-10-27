"""run-all-scenarios.sh must halt when the reset between scenarios fails.

The driver runs from a tmp copy of scripts/ with stub scenario scripts and a stub
reset-scenarios.sh, so no real reset or scenario runs.
"""

import os
import shutil
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"

STUB_SCENARIO = '#!/usr/bin/env bash\necho "{name}" >> "$ATA_TEST_RAN"\nexit 0\n'

# Call 1 is the pre-run baseline reset; every later call (between scenarios) fails.
STUB_RESET = (
    "#!/usr/bin/env bash\n"
    'echo reset >> "$ATA_TEST_RESETS"\n'
    '[ "$(wc -l < "$ATA_TEST_RESETS")" -le 1 ]\n'
)


def make_driver(tmp_path):
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS_DIR, scripts)
    for script in (scripts / "scenarios").glob("s[1-5]_*.sh"):
        script.write_text(STUB_SCENARIO.format(name=script.name.split("_")[0]))
    (scripts / "reset-scenarios.sh").write_text(STUB_RESET)
    for script in [scripts / "reset-scenarios.sh", *(scripts / "scenarios").glob("s[1-5]_*.sh")]:
        script.chmod(0o755)
    return scripts / "run-all-scenarios.sh"


def test_run_all_halts_when_between_scenario_reset_fails(tmp_path):
    driver = make_driver(tmp_path)
    ran, resets = tmp_path / "ran", tmp_path / "resets"
    env = os.environ.copy()
    env.update(SKIP_PREFLIGHT="true", MOCK_MODE="true", ATA_TEST_RAN=str(ran), ATA_TEST_RESETS=str(resets))

    res = subprocess.run([str(driver), "--priority"], cwd=str(tmp_path), capture_output=True,
                         text=True, env=env, timeout=60)

    assert res.returncode == 1, res.stdout + res.stderr
    # Priority order is s4, s1, s3, s5: only s4 runs, then the failed reset stops the driver.
    assert ran.read_text().split() == ["s4"]
    assert resets.read_text().split() == ["reset", "reset"]
    assert "FATAL: reset after S4 failed" in res.stdout
    summary = res.stdout.split("SCENARIO EXECUTION SUMMARY", 1)[1]
    rows = {line.split()[0]: line.split()[-2] for line in summary.splitlines() if line[:2] in ("S1", "S3", "S4", "S5")}
    assert rows == {"S4": "RESET_FAILED", "S1": "UNKNOWN", "S3": "UNKNOWN", "S5": "UNKNOWN"}
    assert "4 scenario(s) failed" in res.stdout


def test_run_all_continues_when_resets_succeed(tmp_path):
    """Control: with a passing reset the same stubs run every priority scenario."""
    driver = make_driver(tmp_path)
    (driver.parent / "reset-scenarios.sh").write_text('#!/usr/bin/env bash\necho reset >> "$ATA_TEST_RESETS"\n')
    ran, resets = tmp_path / "ran", tmp_path / "resets"
    env = os.environ.copy()
    env.update(SKIP_PREFLIGHT="true", MOCK_MODE="true", ATA_TEST_RAN=str(ran), ATA_TEST_RESETS=str(resets))

    res = subprocess.run([str(driver), "--priority"], cwd=str(tmp_path), capture_output=True,
                         text=True, env=env, timeout=60)

    assert res.returncode == 0, res.stdout + res.stderr
    assert ran.read_text().split() == ["s4", "s1", "s3", "s5"]
