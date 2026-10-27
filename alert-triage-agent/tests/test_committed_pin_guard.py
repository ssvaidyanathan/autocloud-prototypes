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
