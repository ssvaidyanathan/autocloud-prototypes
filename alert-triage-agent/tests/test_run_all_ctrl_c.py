"""Ctrl-C under run-all-scenarios.sh stops the whole run (DEMO.md §5).

A real Ctrl-C is typed into a pseudo-terminal: the tty driver sends SIGINT to the whole
foreground process group (the driver, the running scenario, tee and their children), as on
a laptop. Expected: the running scenario finishes gracefully (its output, including NEXT
STEPS, still reaches the terminal), no further scenario and no reset run, the summary marks
the scenario INTERRUPTED and the rest SKIPPED, tells the operator to run
reset-scenarios.sh, and the driver exits 130.

The driver runs from a tmp copy of scripts/ with a stub reset-scenarios.sh, so no real reset
runs. Scenarios run under ATA_TEST_BASH (default: `bash` on PATH) through a `bash` shim first
on PATH, as in test_bash32_compat.py.
"""

import os
import pty
import select
import shutil
import signal
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

# Like a real scenario's hold loop: an INT trap, children in flight when the Ctrl-C lands,
# then a normal finish with next steps (scripts/scenarios/s1_backend_5xx.sh).
STUB_SCENARIO = """#!/usr/bin/env bash
set -euo pipefail
echo "{name}" >> "$ATA_TEST_RAN"
STOP_TRAFFIC=false
trap 'STOP_TRAFFIC=true; echo "{name} interrupted by user (Ctrl-C). Finishing scenario..."' INT TERM
echo "{name} TRAFFIC LOOP"
i=0
while [ "$i" -lt 300 ]; do
    sleep 0.2 || true
    [ "$STOP_TRAFFIC" = true ] && break
    i=$((i + 1))
done
echo "{name} NEXT STEPS"
echo "{name} finished" >> "$ATA_TEST_RAN"
exit 0
"""
STUB_RESET = '#!/usr/bin/env bash\necho reset >> "$ATA_TEST_RESETS"\n'


def make_driver(tmp_path):
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS_DIR, scripts)
    for script in (scripts / "scenarios").glob("s[1-5]_*.sh"):
        script.write_text(STUB_SCENARIO.format(name=script.name.split("_")[0]))
        script.chmod(0o755)
    reset = scripts / "reset-scenarios.sh"
    reset.write_text(STUB_RESET)
    reset.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "bash"
    shim.write_text(f'#!/bin/sh\nexec "{TEST_BASH}" "$@"\n')
    shim.chmod(0o755)
    return scripts / "run-all-scenarios.sh", shim_dir


def run_in_pty_with_ctrl_c(argv, env, cwd, marker, delay=1.0, timeout=60):
    """Run argv on a pty, type Ctrl-C `delay` s after `marker` appears; return (rc, output)."""
    pid, fd = pty.fork()
    if pid == 0:  # child: a non-interactive parent may have left SIGINT ignored
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.chdir(str(cwd))
        os.execvpe(argv[0], argv, env)
    out, t_mark, sent = b"", None, False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                out += data
                if t_mark is None and marker.encode() in out:
                    t_mark = time.monotonic()
            if t_mark is not None and not sent and time.monotonic() - t_mark >= delay:
                os.write(fd, b"\x03")
                sent = True
        else:
            os.kill(pid, signal.SIGKILL)
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert sent, "marker never appeared:\n" + out.decode(errors="replace")
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace").replace("\r\n", "\n")


def summary_rows(output):
    summary = output.split("SCENARIO EXECUTION SUMMARY", 1)[1]
    return {line.split()[0]: line.split()[-2] for line in summary.splitlines() if line[:2] in ("S1", "S2", "S3", "S4", "S5")}


@pytest.fixture
def driver_env(tmp_path):
    assert TEST_BASH, f"ATA_TEST_BASH={os.environ.get('ATA_TEST_BASH')!r} not found"
    driver, shim_dir = make_driver(tmp_path)
    env = os.environ.copy()
    env.update(
        PATH=f"{shim_dir}{os.pathsep}{env.get('PATH', '')}",
        SKIP_PREFLIGHT="true",
        MOCK_MODE="true",
        ATA_TEST_RAN=str(tmp_path / "ran"),
        ATA_TEST_RESETS=str(tmp_path / "resets"),
        TERM="dumb",
    )
    return driver, env, tmp_path


def test_ctrl_c_stops_the_whole_run_without_reset(driver_env):
    driver, env, tmp_path = driver_env

    rc, out = run_in_pty_with_ctrl_c([TEST_BASH, str(driver), "--priority"], env, tmp_path, "s4 TRAFFIC LOOP")

    assert rc == 130, out
    # Priority order is s4, s1, s3, s5: s4 finishes gracefully, nothing else runs.
    assert (tmp_path / "ran").read_text().split() == ["s4", "s4", "finished"]
    # Only the pre-run baseline reset ran; the fault is kept for inspection.
    assert (tmp_path / "resets").read_text().split() == ["reset"]
    # tee survived the Ctrl-C: the scenario's output after it reached the terminal.
    assert "s4 interrupted by user (Ctrl-C). Finishing scenario..." in out
    assert "s4 NEXT STEPS" in out
    assert "Scenario S4 interrupted by Ctrl-C (exit code 0" in out
    assert summary_rows(out) == {"S4": "INTERRUPTED", "S1": "SKIPPED", "S3": "SKIPPED", "S5": "SKIPPED"}
    assert "Run interrupted by Ctrl-C. The environment was NOT reset" in out
    assert "./scripts/reset-scenarios.sh" in out.split("SCENARIO EXECUTION SUMMARY", 1)[1]
    assert "scenario(s) failed" not in out
    # The log file has the scenario's post-Ctrl-C output too.
    log = next(line.split("Log File: ", 1)[1].strip() for line in out.splitlines() if "Log File: " in line)
    try:
        assert "s4 NEXT STEPS" in Path(log).read_text()
    finally:
        Path(log).unlink(missing_ok=True)


def test_ctrl_c_in_a_later_scenario_keeps_earlier_results(driver_env):
    driver, env, tmp_path = driver_env
    # s4 returns at once; the Ctrl-C lands in s1.
    s4 = driver.parent / "scenarios" / next(p.name for p in (driver.parent / "scenarios").glob("s4_*.sh"))
    s4.write_text('#!/usr/bin/env bash\necho s4 >> "$ATA_TEST_RAN"\nexit 0\n')

    rc, out = run_in_pty_with_ctrl_c([TEST_BASH, str(driver), "--priority"], env, tmp_path, "s1 TRAFFIC LOOP")

    assert rc == 130, out
    assert (tmp_path / "ran").read_text().split() == ["s4", "s1", "s1", "finished"]
    # Pre-run reset and the reset between s4 and s1; none after the Ctrl-C.
    assert (tmp_path / "resets").read_text().split() == ["reset", "reset"]
    assert summary_rows(out) == {"S4": "PASSED", "S1": "INTERRUPTED", "S3": "SKIPPED", "S5": "SKIPPED"}
    for line in out.splitlines():
        if "Log File: " in line:
            Path(line.split("Log File: ", 1)[1].strip()).unlink(missing_ok=True)


def test_without_ctrl_c_the_run_is_unchanged(driver_env):
    """Control: the same stubs, no Ctrl-C, run every priority scenario and exit 0."""
    import subprocess

    driver, env, tmp_path = driver_env
    for script in (driver.parent / "scenarios").glob("s[1-5]_*.sh"):
        script.write_text(f'#!/usr/bin/env bash\necho {script.name.split("_")[0]} >> "$ATA_TEST_RAN"\nexit 0\n')

    res = subprocess.run([TEST_BASH, str(driver), "--priority"], cwd=str(tmp_path), capture_output=True,
                         text=True, env=env, timeout=60)

    assert res.returncode == 0, res.stdout + res.stderr
    assert (tmp_path / "ran").read_text().split() == ["s4", "s1", "s3", "s5"]
    assert (tmp_path / "resets").read_text().split() == ["reset"] * 5
    assert "interrupted" not in res.stdout.lower()
    for line in res.stdout.splitlines():
        if "Log File: " in line:
            Path(line.split("Log File: ", 1)[1].strip()).unlink(missing_ok=True)
