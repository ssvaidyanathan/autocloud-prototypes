"""reset-scenarios.sh restocks the demo items (QA r3 R3-5: item-101..103 ran out of stock,
so POST /orders returned HTTP 400 and the scenarios broke).

Every reset (step 5b, before the alert wait) and `--restock-only` set the stock to
INVENTORY_RESTOCK_BASELINE (ATA_INVENTORY_RESTOCK_BASELINE) with POST /admin/restock on the
pinned revision, check the response, read it back with GET /items, and fail loudly
otherwise. Runs under ATA_TEST_BASH with the stateful fake gcloud/curl.
"""

import json

import pytest

from tests.cloud_run_fake import INV_URL, ITEMS, REPO_ROOT, FakeCloud, live_like_state

RESET = REPO_ROOT / "scripts" / "reset-scenarios.sh"
COMMON = REPO_ROOT / "scripts" / "scenarios" / "common.sh"
DEFAULT_BASELINE = {f"item-10{i}": 1000 for i in range(1, 6)}


def restocked_body(stock):
    return {"status": "restocked", "items": [{"id": k, "stock": v} for k, v in sorted(stock.items())]}


@pytest.fixture
def cloud(tmp_path):
    state = live_like_state()
    state["http"][INV_URL]["POST /admin/restock"] = [200, restocked_body(DEFAULT_BASELINE)]
    return FakeCloud(tmp_path, state)


def run_fn(cloud, body, **env):
    script = cloud.tmp_path / "fn.sh"
    script.write_text(f'source "{RESET}"\n{body}\n')
    return cloud.run([script], **env)


def set_http(cloud, key, code, body):
    state = cloud.state
    state["http"][INV_URL][key] = [code, body]
    cloud.set_state(state)


def test_restock_posts_the_baseline_and_verifies_it_twice(cloud):
    res = run_fn(cloud, f'restock_inventory "{INV_URL}" && echo RC0')

    assert "RC0" in res.stdout, res.stdout + res.stderr
    assert "Restocked and verified: item-101=1000" in res.stdout
    bodies = cloud.curl_bodies()
    assert len(bodies) == 1 and bodies[0]["url"] == f"{INV_URL}/admin/restock"
    assert json.loads(bodies[0]["data"]) == {"items": DEFAULT_BASELINE}
    urls = [a for c in cloud.curl_calls() for a in c if a.startswith("http")]
    assert urls == [f"{INV_URL}/admin/restock", f"{INV_URL}/items"]
    post = cloud.curl_calls()[0]
    assert "Content-Type: application/json" in post and "Authorization: Bearer fake-token" in post


def test_restock_uses_the_configured_baseline(cloud):
    set_http(cloud, "POST /admin/restock", 200, restocked_body({"item-105": 700}))
    res = run_fn(cloud, f'restock_inventory "{INV_URL}" && echo RC0',
                 ATA_INVENTORY_RESTOCK_BASELINE="item-105=1000")
    # GET /items reports 1000 for item-105 but the POST answer said 700: loud failure.
    assert "RC0" not in res.stdout
    assert "item-105 stock=700 (want 1000)" in res.stderr
    assert json.loads(cloud.curl_bodies()[0]["data"]) == {"items": {"item-105": 1000}}


def test_missing_endpoint_means_re_pin_first(cloud):
    set_http(cloud, "POST /admin/restock", 404, {"detail": "Not Found"})
    res = run_fn(cloud, f'restock_inventory "{INV_URL}" || echo "RC=$?"')
    assert "RC=1" in res.stdout
    assert "has no restock endpoint" in res.stderr
    assert "scripts/repin-inventory.sh" in res.stderr
    assert "Demo stock was NOT restocked" in res.stderr


def test_unknown_item_in_the_baseline_is_loud(cloud):
    set_http(cloud, "POST /admin/restock", 400, {"detail": {"error": "unknown items", "missing": ["item-999"]}})
    res = run_fn(cloud, f'restock_inventory "{INV_URL}" || echo "RC=$?"')
    assert "RC=1" in res.stdout
    assert "rejected the baseline (HTTP 400)" in res.stderr and "item-999" in res.stderr


@pytest.mark.parametrize("code, text", [(401, "rejected with HTTP 401"), (500, "returned HTTP 500")])
def test_other_restock_failures_are_loud(cloud, code, text):
    set_http(cloud, "POST /admin/restock", code, {"detail": "x"})
    res = run_fn(cloud, f'restock_inventory "{INV_URL}" || echo "RC=$?"')
    assert "RC=1" in res.stdout
    assert text in res.stderr and "NOT restocked" in res.stderr


def test_read_back_mismatch_is_loud(cloud):
    items = [dict(i, stock=0) if i["id"] == "item-101" else i for i in ITEMS]
    set_http(cloud, "GET /items", 200, items)
    res = run_fn(cloud, f'restock_inventory "{INV_URL}" || echo "RC=$?"')
    assert "RC=1" in res.stdout
    assert "item-101 stock=0 (want 1000)" in res.stderr
    assert "GET https://inv.example/items after the restock does not show the baseline stock" in res.stderr


@pytest.mark.parametrize(
    "baseline",
    ["", "item-101", "item-101=0", "item-101=-5", "item-101=10x", "item 101=5", "item-101=5,item-101=6", "=5",
     "item-101=05"],
)
def test_invalid_baseline_is_rejected(cloud, baseline):
    script = cloud.tmp_path / "v.sh"
    script.write_text(f'source "{COMMON}"\nvalidate_restock_baseline && echo VALID\n')
    res = cloud.run([script], ATA_INVENTORY_RESTOCK_BASELINE=baseline)
    if baseline == "":
        # Empty means unset: the default baseline applies.
        assert "VALID" in res.stdout
        return
    assert "VALID" not in res.stdout
    assert "ATA_INVENTORY_RESTOCK_BASELINE" in res.stderr


def test_invalid_baseline_stops_the_reset_before_any_step(cloud):
    res = cloud.run([RESET, "--restock-only"], ATA_INVENTORY_RESTOCK_BASELINE="item-101=zero")
    assert res.returncode != 0
    assert "Invalid ATA_INVENTORY_RESTOCK_BASELINE entry 'item-101=zero'" in res.stderr
    assert cloud.curl_calls() == []


def test_restock_only_restocks_on_the_pinned_revision_and_nothing_else(cloud):
    res = cloud.run([RESET, "--restock-only"])

    assert res.returncode == 0, res.stdout + res.stderr
    urls = [a for c in cloud.curl_calls() for a in c if a.startswith("http")]
    assert urls == [f"{INV_URL}/admin/restock", f"{INV_URL}/items"]  # no fault reset, no alert wait
    assert "Demo inventory restocked" in res.stdout
    assert cloud.calls(["run", "services", "update"]) == []


def test_restock_only_refuses_when_traffic_is_off_the_pin(cloud):
    state = cloud.state
    state["services"]["ata-inventory-api"]["traffic"] = [{"revisionName": "ata-inventory-api-s5-1", "percent": 100}]
    cloud.set_state(state)
    res = cloud.run([RESET, "--restock-only"])
    assert res.returncode != 0
    assert cloud.curl_calls() == []


@pytest.mark.parametrize("mode", ["--dry-run", "--mock"])
def test_restock_only_simulated_makes_no_calls(cloud, mode):
    res = cloud.run([RESET, "--restock-only", mode])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Would POST /admin/restock" in res.stdout
    assert cloud.curl_calls() == [] and cloud.calls() == []


def test_full_reset_restocks_in_step_5b_before_the_alert_wait():
    text = RESET.read_text()
    main = text[text.index('log_info "--> 5b.'):]
    restock = main.index('restock_inventory "${INV_URL}" || exit 1')
    assert "--no-restock: demo stock is NOT restocked" in main[:restock + 200]
    assert main.index('verify_admin_endpoints "${INV_URL}" || exit 1') < restock
    assert restock < main.index("wait_for_open_alerts")


def test_restock_only_and_no_restock_contradict(cloud):
    res = cloud.run([RESET, "--restock-only", "--no-restock"])
    assert res.returncode == 2 and "contradict" in res.stderr
    assert cloud.curl_calls() == []


def test_require_service_url_fails_loudly_instead_of_a_fixed_fallback(cloud):
    state = cloud.state
    state["fail"] = ["describe"]
    cloud.set_state(state)
    script = cloud.tmp_path / "u.sh"
    script.write_text(f'source "{COMMON}"\nU="$(require_service_url ata-inventory-api)"\necho "URL=$U"\n')
    res = cloud.run([script])
    assert res.returncode != 0 and "URL=" not in res.stdout
    assert "Could not read the URL of Cloud Run service ata-inventory-api" in res.stderr


def test_require_service_url_reads_the_live_url(cloud):
    script = cloud.tmp_path / "u.sh"
    script.write_text(f'source "{COMMON}"\necho "URL=$(require_service_url ata-inventory-api)"\n')
    res = cloud.run([script])
    assert res.stdout.strip() == f"URL={INV_URL}", res.stderr
