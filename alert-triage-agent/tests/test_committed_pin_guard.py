"""The conftest guard that keeps tests off the committed inventory pin files (fresh-rev-delta R1).

scripts/scenarios/inventory-pin.<prefix>.env is stack data and changes on every re-pin; a test that
reads it for expected values breaks on the next re-pin. tests/conftest.py fails such a test; these
tests prove that it does, and that the suite's own pin is a fixture file.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import conftest

SCENARIOS = Path(conftest._SCENARIOS_DIR)
COMMITTED = sorted(SCENARIOS.glob("inventory-pin.*.env"))


def _take_violations():
    found = list(conftest._pin_guard["violations"])
    conftest._pin_guard["violations"].clear()
    return found


def test_the_suite_pin_is_a_fixture_file_not_a_committed_one():
    pin = os.environ["ATA_INVENTORY_PIN_FILE"]
    assert pin == conftest.TEST_PIN_FILE
    assert not conftest._is_committed_pin(pin)
    assert Path(pin).read_text().count(f"PIN_INVENTORY_REVISION={conftest.TEST_PIN_REVISION}\n") == 1


@pytest.mark.skipif(not COMMITTED, reason="no committed pin file in this checkout")
def test_opening_a_committed_pin_is_recorded(tmp_path):
    COMMITTED[0].read_text()
    shutil.copy(COMMITTED[0], tmp_path / "copy.env")
    found = _take_violations()
    assert len(found) == 2 and all(f"opened {COMMITTED[0]}" == v for v in found), found


@pytest.mark.skipif(not COMMITTED, reason="no committed pin file in this checkout")
def test_a_script_run_that_would_read_a_committed_pin_is_recorded(tmp_path):
    prefix = COMMITTED[0].name[len("inventory-pin."):-len(".env")]
    script = tmp_path / "src.sh"
    script.write_text(f'source "{SCENARIOS}/common.sh"\n')
    env = dict(os.environ, NAME_PREFIX=prefix, ATA_INVENTORY_PIN_FILE="")
    subprocess.run(["true", str(script)], env=env, check=True)  # names the checkout's common.sh
    subprocess.run(["true", str(SCENARIOS / "preflight.sh")], env=env, check=True)  # a checkout script
    subprocess.run(["true"], env=dict(env, ATA_INVENTORY_PIN_FILE=str(COMMITTED[0])), check=True)
    found = _take_violations()
    assert len(found) == 3, found
    assert "no ATA_INVENTORY_PIN_FILE" in found[0] and "no ATA_INVENTORY_PIN_FILE" in found[1]
    assert found[2] == f"subprocess ATA_INVENTORY_PIN_FILE={COMMITTED[0]}"


# --- Q2 (fresh-rev-r2): the bypasses the re-review found ------------------------------------
# Each process below only echoes, or exits on a missing file: the guard decides from the argv,
# env and cwd alone, so nothing here actually reads a committed pin's values.
needs_pin = pytest.mark.skipif(not COMMITTED, reason="no committed pin file in this checkout")


def _prefix():
    return COMMITTED[0].name[len("inventory-pin."):-len(".env")]


@needs_pin
@pytest.mark.parametrize("form", ["argv-list", "string-shell", "relative-to-cwd", "relative-to-test-cwd",
                                  "shell-words", "glob"])
def test_a_process_that_names_a_committed_pin_is_recorded_whatever_the_pin_env(form, monkeypatch):
    """`cat <pin>` with the suite's fixture ATA_INVENTORY_PIN_FILE set (the default env) was not caught."""
    pin = COMMITTED[0]
    rel = os.path.relpath(pin, conftest._CHECKOUT)
    kw = {}
    if form == "argv-list":
        cmd = ["echo", str(pin)]
    elif form == "string-shell":
        cmd, kw = f"echo {pin} >/dev/null", {"shell": True}
    elif form == "relative-to-cwd":
        cmd, kw = ["echo", rel], {"cwd": conftest._CHECKOUT}
    elif form == "relative-to-test-cwd":
        monkeypatch.chdir(SCENARIOS)
        cmd = ["echo", pin.name]
    elif form == "shell-words":
        cmd, kw = ["bash", "-c", f"X={rel}; echo \"$X\""], {"cwd": conftest._CHECKOUT}
    else:
        cmd, kw = ["bash", "-c", "echo scripts/scenarios/inventory-pin.*.env"], {"cwd": conftest._CHECKOUT}
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, **kw)
    found = _take_violations()
    assert len(found) == 1 and "names the committed pin" in found[0], found


@needs_pin
def test_a_checkout_script_given_relative_to_the_cwd_is_recorded(tmp_path):
    """bash -c 'source scripts/scenarios/common.sh' with cwd=<checkout> and no pin file: reads the committed pin."""
    env = dict(os.environ, NAME_PREFIX=_prefix(), ATA_INVENTORY_PIN_FILE="")
    subprocess.run(["true", "source scripts/scenarios/common.sh; echo"], cwd=conftest._CHECKOUT, env=env, check=True)
    subprocess.run(["true", "common.sh"], cwd=str(SCENARIOS), env=env, check=True)
    found = _take_violations()
    assert len(found) == 2 and all("no ATA_INVENTORY_PIN_FILE" in v for v in found), found
    # The same relative words from a cwd outside the checkout name nothing of it.
    subprocess.run(["true", "source scripts/scenarios/common.sh; echo"], cwd=str(tmp_path), env=env, check=True)
    assert _take_violations() == []


@needs_pin
def test_os_system_is_checked(monkeypatch):
    rc = os.system(f"true {COMMITTED[0]}")
    assert rc == 0
    found = _take_violations()
    assert len(found) == 1 and "names the committed pin" in found[0], found
    monkeypatch.setenv("NAME_PREFIX", _prefix())
    monkeypatch.setenv("ATA_INVENTORY_PIN_FILE", "")
    assert os.system(f"true {SCENARIOS / 'preflight.sh'}") == 0
    found = _take_violations()
    assert len(found) == 1 and "no ATA_INVENTORY_PIN_FILE" in found[0], found
    monkeypatch.setenv("ATA_INVENTORY_PIN_FILE", conftest.TEST_PIN_FILE)
    assert os.system(f"true {SCENARIOS / 'preflight.sh'}") == 0
    assert _take_violations() == []


@needs_pin
def test_os_posix_spawn_is_checked_once():
    pid = os.posix_spawn("/bin/echo", ["echo", str(COMMITTED[0])], dict(os.environ),
                         file_actions=[(os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0)])
    assert os.waitpid(pid, 0)[1] == 0
    found = _take_violations()
    assert len(found) == 1 and "names the committed pin" in found[0], found
    # subprocess may itself use posix_spawn (absolute executable, close_fds=False): one record, not two.
    subprocess.run(["/bin/echo", str(COMMITTED[0])], close_fds=False, check=True, stdout=subprocess.DEVNULL)
    assert len(_take_violations()) == 1


def test_os_exec_is_hooked():
    """os.exec* replaces the process, so it is not run here: the hook gets the exec audit event."""
    pin = COMMITTED[0] if COMMITTED else None
    conftest._pin_audit_hook("os.exec", ("/bin/cat", ["cat", str(pin or "/nonexistent")], dict(os.environ)))
    found = _take_violations()
    assert len(found) == (1 if pin else 0), found


def test_naming_a_fixture_pin_or_a_tmp_copy_is_not_recorded(tmp_path):
    copy = tmp_path / "scripts" / "scenarios" / "inventory-pin.ata.env"
    copy.parent.mkdir(parents=True)
    copy.write_text("x\n")
    subprocess.run(["cat", str(copy)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["cat", "scripts/scenarios/inventory-pin.ata.env"], cwd=str(tmp_path), check=True,
                   stdout=subprocess.DEVNULL)
    assert os.system(f"cat {copy} >/dev/null") == 0
    assert _take_violations() == []


def test_runs_that_use_a_fixture_pin_are_not_recorded(tmp_path):
    pin = tmp_path / "inventory-pin.ata.env"
    pin.write_text("x\n")
    pin.read_text()
    subprocess.run(["true", str(SCENARIOS / "preflight.sh")], env=dict(os.environ, ATA_INVENTORY_PIN_FILE=str(pin)), check=True)
    # A prefix without a committed pin, and a copy of scripts/ under tmp_path: nothing to read.
    subprocess.run(["true", str(SCENARIOS / "preflight.sh")],
                   env=dict(os.environ, ATA_INVENTORY_PIN_FILE="", NAME_PREFIX="zzq"), check=True)
    subprocess.run(["true", str(tmp_path / "scripts" / "scenarios" / "preflight.sh")],
                   env=dict(os.environ, ATA_INVENTORY_PIN_FILE=""), check=True)
    assert _take_violations() == []


@pytest.mark.reads_committed_pin
@pytest.mark.skipif(not COMMITTED, reason="no committed pin file in this checkout")
def test_the_marker_allows_a_format_check():
    assert COMMITTED[0].read_text()
    assert _take_violations() == []
