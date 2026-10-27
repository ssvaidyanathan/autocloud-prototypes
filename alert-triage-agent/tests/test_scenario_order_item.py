"""select_order_item (scripts/scenarios/common.sh): S4 order traffic uses an in-stock item.

QA S4: every POST /orders for item-101 returned HTTP 400 (its stock was used up by earlier
runs), so the orders path never exercised the inventory service. The helper is tested by
sourcing common.sh under ATA_TEST_BASH with curl stubbed to print a fixed GET /items body.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

COMMON = Path(__file__).parent.parent / "scripts" / "scenarios" / "common.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))


def _run(tmp_path, body_text, expected=0):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "items.json").write_text(body_text)
    curl = bindir / "curl"
    curl.write_text('#!/bin/sh\necho "$*" >> "$PWD/curl_calls"\ncat "$PWD/items.json"\n')
    curl.chmod(0o755)
    script = tmp_path / "run.sh"
    script.write_text(
        f'source "{COMMON}"\nAPIGEE_HOST=apigee.example\n'
        f'item="$(select_order_item test-key {expected})" || {{ echo "RC=$?"; exit 0; }}\necho "ITEM=$item"\n'
    )
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", MOCK_MODE="false", DRY_RUN="false")
    return subprocess.run([TEST_BASH, str(script)], cwd=str(tmp_path), capture_output=True, text=True,
                          env=env, timeout=30)


ITEMS = [
    {"id": "item-101", "stock": 0},
    {"id": "item-103", "stock": 290},
    {"id": "item-105", "stock": 480},
]


def test_picks_the_in_stock_item_with_the_most_stock(tmp_path):
    res = _run(tmp_path, json.dumps(ITEMS), expected=420)
    assert "ITEM=item-105" in res.stdout, res.stdout + res.stderr
    assert "WARN" not in res.stderr
    call = (tmp_path / "curl_calls").read_text()
    assert "https://apigee.example/ata/inventory/v1/items" in call and "x-api-key: test-key" in call


def test_warns_when_stock_is_below_the_orders_sent(tmp_path):
    res = _run(tmp_path, json.dumps(ITEMS), expected=600)
    assert "ITEM=item-105" in res.stdout
    assert "item-105 has stock 480, below the ~600 orders this run sends" in res.stderr


@pytest.mark.parametrize("body", [json.dumps([{"id": "item-101", "stock": 0}]), "200", ""])
def test_fails_loudly_without_an_in_stock_item(tmp_path, body):
    res = _run(tmp_path, body)
    assert "RC=1" in res.stdout
    assert "ITEM=" not in res.stdout
    assert "Could not find an in-stock item" in res.stderr
    assert "would return HTTP 400" in res.stderr


def test_s4_sends_the_selected_item_not_a_fixed_one():
    s4 = (COMMON.parent / "s4_db_connection_pool.sh").read_text()
    assert '"item_id":"item-101"' not in s4.replace('ORDER_ITEM="item-101"', "")
    assert 'select_order_item "${API_KEY}" "${HOLD_SECS}"' in s4
    assert '\\"item_id\\":\\"${ORDER_ITEM}\\"' in s4


# Review N2: a non-integer stock (e.g. 12.0) used to make `[ -gt ]` fail silently, so the
# low-stock warning was skipped. The stock is now coerced with int().
@pytest.mark.parametrize("stock", [12.0, "12", "12.0", 12.7])
def test_non_integer_stock_is_coerced_and_still_warns(tmp_path, stock):
    res = _run(tmp_path, json.dumps([{"id": "item-103", "stock": stock}]), expected=600)
    assert "ITEM=item-103" in res.stdout, res.stdout + res.stderr
    assert "item-103 has stock 12, below the ~600 orders this run sends" in res.stderr


def test_unparseable_stock_counts_as_not_in_stock(tmp_path):
    body = json.dumps([{"id": "item-101", "stock": "lots"}, {"id": "item-103", "stock": 5.0}])
    res = _run(tmp_path, body, expected=10)
    assert "ITEM=item-103" in res.stdout, res.stdout + res.stderr
    assert "item-103 has stock 5, below the ~10 orders" in res.stderr


@pytest.mark.parametrize("stock", ["lots", None, [1]])
def test_only_unparseable_stock_fails_loudly(tmp_path, stock):
    res = _run(tmp_path, json.dumps([{"id": "item-101", "stock": stock}]))
    assert "RC=1" in res.stdout
    assert "Could not find an in-stock item" in res.stderr
