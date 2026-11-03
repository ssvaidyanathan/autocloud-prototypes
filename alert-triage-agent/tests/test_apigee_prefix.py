"""Prefix-derived Apigee bundles, scripts/deploy-apigee-proxies.sh and scripts/teardown-apigee.sh.

Nothing here reaches Apigee or gcloud: both are stubs on PATH. The curl stub is a small fake
of the Apigee management API (stateful across one test, so teardown sees its own deletes),
and records every call as "METHOD URL" in $STUB_CALLS. Responses can be overridden per test
with $STUB_RULES (JSON list of {"method", "match" (substring of the URL), "code", "body"}).

Run with a real bash 3.2: ATA_TEST_BASH=/path/to/bash-3.2.57/bash pytest tests/test_apigee_prefix.py
"""

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._stubs import write_stub

ROOT = Path(__file__).parent.parent
BUNDLES = ROOT / "demo" / "apigee"
RENDER = BUNDLES / "render-bundle.sh"
DEPLOY = ROOT / "scripts" / "deploy-apigee-proxies.sh"
TEARDOWN = ROOT / "scripts" / "teardown-apigee.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

# SHA-256 of every file of the legacy bundles demo/apigee/ata-{orders,inventory}-v1 at 5ef9b7c.
# The `ata` render must reproduce them byte for byte.
LEGACY_SHA256 = {
    "orders-v1": {
        "apiproxy/ata-orders-v1.xml": "fb8c51a096c55a5c413c1e75e077681c72540ae224a5c9b4159b95ed76f9a507",
        "apiproxy/policies/ML-CloudLogging.xml": "d7ad1a434dab170e62d461036eebc6ec392972b8c6402d2a9d17ac57f0454811",
        "apiproxy/policies/RF-BlockAdmin.xml": "295daadbdd3deff3f1dc1842e1817b12fa7a778025919359d9dfe8fd3de43771",
        "apiproxy/policies/SA-SpikeArrest.xml": "e6340dbcf9545acfc81ac5e5a887f5393b4dfd6513a36f8ffd4b236cfded46ac",
        "apiproxy/policies/VA-VerifyKey-Header.xml": "252babd82b845216e0b193c562af05706284195a3055a14e2e4720357e235c36",
        "apiproxy/policies/VA-VerifyKey-Query.xml": "40fd276725088e29d5e159124c9dc3f81ae6dcaa6a9df6f6074dfe858fc318f0",
        "apiproxy/proxies/default.xml": "de8b81c097add500ddc8cbcc08b2ef47de259172e129038b67560e218929b36f",
        "apiproxy/targets/default.xml": "c28c620eb688f1c6c23d82882a00d016c75d96601a9ecce3db655cf4ec4c40f4"
    },
    "inventory-v1": {
        "apiproxy/ata-inventory-v1.xml": "2043430bcc0623db2378c65e84701aa74c0ff35356fe2bbea815d53b43a0db75",
        "apiproxy/policies/ML-CloudLogging.xml": "d7ad1a434dab170e62d461036eebc6ec392972b8c6402d2a9d17ac57f0454811",
        "apiproxy/policies/RF-BlockAdmin.xml": "295daadbdd3deff3f1dc1842e1817b12fa7a778025919359d9dfe8fd3de43771",
        "apiproxy/policies/SA-SpikeArrest.xml": "e6340dbcf9545acfc81ac5e5a887f5393b4dfd6513a36f8ffd4b236cfded46ac",
        "apiproxy/policies/VA-VerifyKey-Header.xml": "252babd82b845216e0b193c562af05706284195a3055a14e2e4720357e235c36",
        "apiproxy/policies/VA-VerifyKey-Query.xml": "40fd276725088e29d5e159124c9dc3f81ae6dcaa6a9df6f6074dfe858fc318f0",
        "apiproxy/proxies/default.xml": "11b3204322ba163a4cff19bd38d2430047b03d85df8efa1e2ff9b40902333c59",
        "apiproxy/targets/default.xml": "2515efb0c4eab458126e6bf7287de93de321c0837e9dd5023d3fff2e6cba78fb"
    }
}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def render(tmp_path, template, prefix, name=None, zip_file=None):
    out = tmp_path / (name or f"{prefix}-{template}")
    cmd = [TEST_BASH, str(RENDER), template, prefix, str(out)] + ([str(zip_file)] if zip_file else [])
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return res, out


# ---------------------------------------------------------------- bundle rendering


@pytest.mark.parametrize("prefix", ["ata", "atat"])
@pytest.mark.parametrize("api", ["orders", "inventory"])
def test_render_names_and_base_paths(tmp_path, prefix, api):
    res, out = render(tmp_path, f"{api}-v1", prefix, zip_file=tmp_path / "b.zip")
    assert res.returncode == 0, res.stderr
    files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert f"apiproxy/{prefix}-{api}-v1.xml" in files
    blob = "".join((out / f).read_text() for f in files)
    assert "@PREFIX" not in blob and "@" not in "".join(files)
    root = (out / f"apiproxy/{prefix}-{api}-v1.xml").read_text()
    assert f'<APIProxy revision="1" name="{prefix}-{api}-v1">' in root
    assert f"<DisplayName>{prefix}-{api}-v1</DisplayName>" in root
    assert f"{prefix.upper()} {api.capitalize()} Proxy v1 [app=alert-triage-agent,component={prefix}-{api}-v1," in root
    assert f"<BasePath>/{prefix}/{api}/v1</BasePath>" in (out / "apiproxy/proxies/default.xml").read_text()
    assert f'<Server name="{prefix}-{api}-target"/>' in (out / "apiproxy/targets/default.xml").read_text()
    assert f"/logs/{prefix}-apigee-access</LogName>" in (out / "apiproxy/policies/ML-CloudLogging.xml").read_text()
    if prefix == "atat":
        assert "ata-" not in blob and "/ata/" not in blob and "ATA " not in blob
    import zipfile
    with zipfile.ZipFile(tmp_path / "b.zip") as zf:
        assert sorted(zf.namelist()) == files



def test_ata_render_is_byte_identical_to_legacy_bundles(tmp_path):
    for template, digests in LEGACY_SHA256.items():
        res, out = render(tmp_path, template, "ata")
        assert res.returncode == 0, res.stderr
        got = {str(p.relative_to(out)): sha(p) for p in out.rglob("*") if p.is_file()}
        assert got == digests, template


@pytest.mark.parametrize("bad", ["", "a", "Ata", "1ab", "at-a", "at_a", "abcdefghijk", "ata ", "ätä"])
def test_render_rejects_invalid_prefix(tmp_path, bad):
    res, out = render(tmp_path, "orders-v1", bad, name="out")
    assert res.returncode != 0
    assert "invalid prefix" in res.stderr
    assert not out.exists()


def test_render_fails_on_leftover_placeholder(tmp_path):
    fake = tmp_path / "apigee"
    shutil.copytree(BUNDLES, fake)
    (fake / "orders-v1/apiproxy/policies/X.xml").write_text("<X>@PREFIX_TYPO@</X>\n")
    res = subprocess.run([TEST_BASH, str(fake / "render-bundle.sh"), "orders-v1", "atat", str(tmp_path / "o")],
                         capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "placeholder left" in res.stderr


def test_templates_contain_no_prefix_literal():
    for p in BUNDLES.rglob("*.xml"):
        text = p.read_text()
        assert "ata-" not in text and "/ata/" not in text and "ATA" not in text, p


def test_legacy_bundle_dirs_are_gone():
    assert not (BUNDLES / "ata-orders-v1").exists() and not (BUNDLES / "ata-inventory-v1").exists()


# ---------------------------------------------------------------- stubs

CURL_STUB = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
method, out, data, url = "GET", None, "", None
i = 0
while i < len(args):
    a = args[i]
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "-o": out = args[i + 1]; i += 2; continue
    if a in ("-w", "-H", "-F"): i += 2; continue
    if a == "--config":
        with open(os.environ["STUB_CALLS"] + ".config", "a") as f:
            f.write(open(args[i + 1]).read())
        i += 2; continue
    if a == "-d": data = args[i + 1]; i += 2; continue
    if a.startswith("-"): i += 1; continue
    url = a; i += 1
with open(os.environ["STUB_CALLS"] + ".argv", "a") as f:
    f.write(json.dumps(args) + "\n")
state_file = os.environ["STUB_STATE"]
state = json.load(open(state_file)) if os.path.exists(state_file) else {"deleted": [], "created": []}
with open(os.environ["STUB_CALLS"], "a") as f:
    f.write(f"{method} {url}\n")
path = url.split("?")[0]
code, body = 200, {}
for rule in json.loads(os.environ.get("STUB_RULES", "[]")):
    if rule["method"] == method and rule["match"] in url + "?":
        rule_hit = rule
        code, body = rule["code"], rule.get("body", {})
        break
else:
    rule_hit = None
if rule_hit is None:
    gone = lambda p: any(p == d or p.startswith(d + "/") for d in state["deleted"])
    if method == "DELETE":
        if gone(path) or os.environ.get("STUB_ABSENT"):
            code, body = 404, {"error": {"code": 404}}
        else:
            state["deleted"].append(path)
    elif method == "GET":
        m = re.search(r"/apis/([^/]+)/deployments$", path)
        if m:
            dep = re.sub(r"/deployments$", "", path) + "/revisions/3/deployments"
            gone_proxy = gone(path.rsplit("/environments/", 1)[0] + "/apis/" + m.group(1))
            if os.environ.get("STUB_ABSENT") or gone_proxy:
                code, body = 404, {}
            else:
                body = {"deployments": [] if gone(dep) else [{"revision": "3"}]}
        elif re.search(r"/revisions/\d+/deployments$", path):
            body = {"state": os.environ.get("STUB_DEPLOY_STATE", "READY")}
        elif re.search(r"/(apis|apiproducts|developers|targetservers)$", path):
            coll = path.rsplit("/", 1)[1]
            lists = json.loads(os.environ.get("STUB_LIST", "{}"))
            names = lists.get(coll, {"apis": ["ata-orders-v1", "httpbin"], "apiproducts": ["ata-product"],
                                     "developers": ["ata-dev@example.com"],
                                     "targetservers": ["ata-orders-target"]}[coll])
            names = [n for n in names if not gone(path + "/" + n)]
            key = {"apis": "proxies", "apiproducts": "apiProduct", "developers": "developer"}.get(coll)
            field = "email" if coll == "developers" else "name"
            body = names if key is None else {key: [{field: n} for n in names]}
        elif os.environ.get("STUB_ABSENT") or gone(path):
            code, body = 404, {"error": {"code": 404}}
    elif method == "POST":
        if "action=import" in url:
            body = {"name": re.search(r"name=([^&]+)", url).group(1), "revision": "7"}
        elif path.endswith("/apps"):
            d = json.loads(data)
            body = {"name": d["name"], "credentials": [{"consumerKey": "key-" + d["name"], "status": "approved",
                                                        "apiProducts": [{"apiproduct": p} for p in d["apiProducts"]]}]}
            code = 201
        elif re.search(r"/(targetservers|apiproducts|developers)$", path):
            code = 201
json.dump(state, open(state_file, "w"))
if out:
    open(out, "w").write(body if isinstance(body, str) else json.dumps(body))
sys.stdout.write(str(code))
'''

GCLOUD_STUB = r'''#!/bin/sh
printf '%s\n' "gcloud $*" >> "$STUB_CALLS"
case "$1 $2" in
  "auth print-access-token" | "auth application-default") echo "stub-token" ;;
  "run services")
     svc="$4"
     [ -f "$STUB_DIR/fail-$svc" ] && { echo "ERROR: describe failed" >&2; exit 1; }
     [ -f "$STUB_DIR/empty-$svc" ] && exit 0
     printf '%s\n' "https://$svc-abc123-uw.a.run.app" ;;
esac
'''


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to(TEST_BASH)
    for name, body in [("curl", CURL_STUB), ("gcloud", GCLOUD_STUB)]:
        write_stub(bin_dir / name, body)
    out = tmp_path / "ata-dir"
    out.mkdir()
    keep = {k: v for k, v in os.environ.items()
            if k not in ("PROJECT_ID", "REGION", "NAME_PREFIX", "APIGEE_ORG", "APIGEE_ENV", "APIGEE_HOST", "ENV_NAME")}
    keep.update(PATH=f"{bin_dir}{os.pathsep}{keep['PATH']}", STUB_CALLS=str(tmp_path / "calls"),
                STUB_STATE=str(tmp_path / "state.json"), STUB_DIR=str(tmp_path), TMPDIR=str(tmp_path),
                PROJECT_ID="proj-x", REGION="region-x", NAME_PREFIX="atat", APIGEE_ORG="org-x",
                APIGEE_ENV="env-x", APIGEE_HOST="host.example.test", ATA_STACK_DIR=str(out),
                DEPLOY_POLL_SECS="0", DEPLOY_TIMEOUT_SECS="3", UNDEPLOY_TIMEOUT_SECS="3")
    return keep


def run(script, env, *args, stdin=None):
    return subprocess.run([TEST_BASH, str(script), *args], env=env, capture_output=True, text=True,
                          input=stdin, timeout=120, check=False)


def calls(env):
    p = Path(env["STUB_CALLS"])
    return [line for line in p.read_text().splitlines() if not line.startswith("gcloud ")] if p.exists() else []


def mutating(env):
    return [c for c in calls(env) if c.split(" ", 1)[0] in ("POST", "PUT", "DELETE")]


# ---------------------------------------------------------------- deploy


@pytest.mark.parametrize("var", ["PROJECT_ID", "REGION", "NAME_PREFIX", "APIGEE_ENV", "APIGEE_HOST"])
def test_deploy_requires_each_input(env, var):
    del env[var]
    res = run(DEPLOY, env)
    assert res.returncode != 0
    assert f"{var} is required" in res.stderr
    assert calls(env) == [] and not Path(env["STUB_CALLS"]).exists()


@pytest.mark.parametrize("bad", ["ATA", "a", "ata-x", "abcdefghijk"])
def test_deploy_rejects_invalid_prefix(env, bad):
    env["NAME_PREFIX"] = bad
    res = run(DEPLOY, env)
    assert res.returncode != 0 and "must match" in res.stderr
    assert not Path(env["STUB_CALLS"]).exists()


def test_deploy_ignores_legacy_env_name(env):
    del env["APIGEE_ENV"]
    env["ENV_NAME"] = "prod"
    res = run(DEPLOY, env)
    assert res.returncode != 0 and "APIGEE_ENV is required" in res.stderr


def test_deploy_happy_path_touches_only_prefixed_resources(env):
    res = run(DEPLOY, env)
    assert res.returncode == 0, res.stdout + res.stderr
    all_calls = calls(env)
    assert all("/ata-" not in c and "/ata/" not in c for c in all_calls), all_calls
    assert all(c.split(" ", 1)[1].startswith("https://apigee.googleapis.com/v1/organizations/org-x") for c in all_calls)
    base = "https://apigee.googleapis.com/v1/organizations/org-x"
    sa = "atat-apigee-sa@proj-x.iam.gserviceaccount.com"
    assert mutating(env) == [
        f"POST {base}/environments/env-x/targetservers",
        f"POST {base}/environments/env-x/targetservers",
        f"POST {base}/apis?name=atat-orders-v1&action=import",
        f"POST {base}/environments/env-x/apis/atat-orders-v1/revisions/7/deployments?override=true&serviceAccount=projects/-/serviceAccounts/{sa}",
        f"POST {base}/apis?name=atat-inventory-v1&action=import",
        f"POST {base}/environments/env-x/apis/atat-inventory-v1/revisions/7/deployments?override=true&serviceAccount=projects/-/serviceAccounts/{sa}",
        f"POST {base}/apiproducts",
        f"POST {base}/developers",
        f"POST {base}/developers/atat-dev@example.com/apps",
    ]
    # TargetServer hosts came from the prefixed Cloud Run services.
    gcalls = [line for line in Path(env["STUB_CALLS"]).read_text().splitlines() if line.startswith("gcloud run")]
    assert [g.split()[4] for g in gcalls] == ["atat-orders-api", "atat-inventory-api"]
    assert "--project=proj-x" in gcalls[0] and "--region=region-x" in gcalls[0]
    # The exact plan is printed before anything is mutated (the plan precedes the token fetch).
    plan = res.stdout.split("will be created or updated:")[1].split("Local files:")[0]
    for name in ["env-x/atat-orders-target -> atat-orders-api-abc123-uw.a.run.app:443",
                 "env-x/atat-inventory-target -> atat-inventory-api-abc123-uw.a.run.app:443",
                 "atat-orders-v1 (new revision", "atat-inventory-v1 (new revision", "apiproduct    atat-product",
                 "developer     atat-dev@example.com", "app           atat-dev@example.com/atat-demo-app"]:
        assert name in plan, name
    assert res.stdout.index("will be created or updated") < res.stdout.index("Imported revision")
    key = Path(env["ATA_STACK_DIR"]) / ".atat-api-key"
    assert key.read_text() == "key-atat-demo-app\n"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert (Path(env["ATA_STACK_DIR"]) / ".atat-apigee.env").read_text() == \
        "APIGEE_HOST=host.example.test\nAPIGEE_ORG=org-x\nAPIGEE_ENV=env-x\n"
    assert "key-atat-demo-app" not in res.stdout
    assert "/atat/orders/v1" in res.stdout and "/atat/inventory/v1" in res.stdout


def test_deploy_older_alert_triage_dir_name_still_sets_the_file_dir(env, tmp_path):
    """ALERT_TRIAGE_DIR is the older name of ATA_STACK_DIR (which wins when both are set)."""
    old = tmp_path / "old-name"
    old.mkdir()
    out = Path(env.pop("ATA_STACK_DIR"))
    assert run(DEPLOY, dict(env, ALERT_TRIAGE_DIR=str(old))).returncode == 0
    assert (old / ".atat-api-key").is_file() and (old / ".atat-apigee.env").is_file()
    assert not (out / ".atat-api-key").exists()
    assert run(DEPLOY, dict(env, ALERT_TRIAGE_DIR=str(old), ATA_STACK_DIR=str(out))).returncode == 0
    assert (out / ".atat-api-key").is_file() and (out / ".atat-apigee.env").is_file()


def test_deploy_rejects_a_stack_dir_that_is_not_a_directory(env, tmp_path):
    res = run(DEPLOY, dict(env, ATA_STACK_DIR=str(tmp_path / "missing")))
    assert res.returncode != 0 and "is not a directory" in res.stderr


def test_deploy_env_file_is_sourceable(env):
    assert run(DEPLOY, env).returncode == 0
    out = subprocess.run([TEST_BASH, "-c", f'. "{env["ATA_STACK_DIR"]}/.atat-apigee.env"; '
                          'printf "%s|%s|%s" "$APIGEE_HOST" "$APIGEE_ORG" "$APIGEE_ENV"'],
                         capture_output=True, text=True, check=True, env={"PATH": env["PATH"]})
    assert out.stdout == "host.example.test|org-x|env-x"


def test_deploy_org_defaults_to_project_and_says_so(env):
    del env["APIGEE_ORG"]
    res = run(DEPLOY, env)
    assert res.returncode == 0, res.stderr
    assert "using PROJECT_ID as the Apigee organization: proj-x" in res.stdout
    assert all("/organizations/proj-x/" in c for c in calls(env))


@pytest.mark.parametrize("svc", ["atat-orders-api", "atat-inventory-api"])
@pytest.mark.parametrize("how", ["empty", "fail"])
def test_deploy_fails_without_targetserver_host(env, tmp_path, svc, how):
    (tmp_path / f"{how}-{svc}").write_text("")
    res = run(DEPLOY, env)
    assert res.returncode != 0
    assert svc in res.stderr
    assert "4klvlqnmpa" not in res.stdout + res.stderr
    assert calls(env) == []  # no management API call at all


def test_deploy_fails_on_non_2xx_and_prints_body(env):
    env["STUB_RULES"] = json.dumps([{"method": "POST", "match": "/targetservers", "code": 403,
                                     "body": {"error": {"message": "caller lacks apigee.targetservers.create"}}}])
    res = run(DEPLOY, env)
    assert res.returncode != 0
    assert "HTTP 403" in res.stderr and "caller lacks apigee.targetservers.create" in res.stderr
    assert not any("import" in c for c in calls(env))
    assert not (Path(env["ATA_STACK_DIR"]) / ".atat-api-key").exists()


def test_deploy_fails_when_environment_missing(env):
    env["STUB_RULES"] = json.dumps([{"method": "GET", "match": "/environments/env-x", "code": 404,
                                     "body": {"error": {"message": "environment not found"}}}])
    res = run(DEPLOY, env)
    assert res.returncode != 0 and "environment not found" in res.stderr
    assert mutating(env) == []


def test_deploy_409_means_exists_then_put(env):
    base = "https://apigee.googleapis.com/v1/organizations/org-x"
    env["STUB_RULES"] = json.dumps([
        {"method": "POST", "match": "/targetservers", "code": 409, "body": {"error": {"message": "exists"}}},
        {"method": "POST", "match": "/apiproducts", "code": 409, "body": {}},
        {"method": "POST", "match": f"{base}/developers?", "code": 409, "body": {}},
    ])
    res = run(DEPLOY, env)
    assert res.returncode == 0, res.stderr
    puts = [c for c in mutating(env) if c.startswith("PUT ")]
    assert puts == [f"PUT {base}/environments/env-x/targetservers/atat-orders-target",
                    f"PUT {base}/environments/env-x/targetservers/atat-inventory-target",
                    f"PUT {base}/apiproducts/atat-product",
                    f"PUT {base}/developers/atat-dev@example.com"]


def test_deploy_409_followed_by_failing_put_fails(env):
    env["STUB_RULES"] = json.dumps([
        {"method": "POST", "match": "/targetservers", "code": 409, "body": {}},
        {"method": "PUT", "match": "/targetservers/", "code": 500, "body": {"error": {"message": "boom"}}},
    ])
    res = run(DEPLOY, env)
    assert res.returncode != 0 and "boom" in res.stderr and "HTTP 500" in res.stderr


def test_deploy_import_without_revision_fails(env):
    env["STUB_RULES"] = json.dumps([{"method": "POST", "match": "action=import", "code": 200, "body": {"name": "x"}}])
    res = run(DEPLOY, env)
    assert res.returncode != 0 and "no numeric revision" in res.stderr
    assert not any("/deployments" in c for c in mutating(env))


def test_deploy_rejected_deployment_fails(env):
    env["STUB_RULES"] = json.dumps([{"method": "POST", "match": "/deployments?override=true", "code": 400,
                                     "body": {"error": {"message": "service account not found"}}}])
    res = run(DEPLOY, env)
    assert res.returncode != 0 and "service account not found" in res.stderr
    assert "atat-apigee-sa@proj-x.iam.gserviceaccount.com" in res.stderr


@pytest.mark.parametrize("state,msg", [("PROGRESSING", "Waiting 3s"), ("ERROR", "state ERROR")])
def test_deploy_polls_until_ready_or_fails(env, state, msg):
    env["STUB_DEPLOY_STATE"] = state
    res = run(DEPLOY, env)
    assert res.returncode != 0 and msg in res.stderr, res.stderr


def test_deploy_existing_app_key_gets_product(env):
    app = {"name": "atat-demo-app", "credentials": [{"consumerKey": "oldkey", "status": "approved", "apiProducts": []}]}
    app_with = {"name": "atat-demo-app", "credentials": [
        {"consumerKey": "oldkey", "status": "approved", "apiProducts": [{"apiproduct": "atat-product"}]}]}
    env["STUB_RULES"] = json.dumps([
        {"method": "POST", "match": "/apps/atat-demo-app/keys/oldkey", "code": 200, "body": {}},
        {"method": "POST", "match": "/developers/atat-dev@example.com/apps", "code": 409, "body": {}},
        {"method": "GET", "match": "/apps/atat-demo-app", "code": 200, "body": app},
    ])
    res = run(DEPLOY, env)
    # The stub returns the same app body on the re-read, so the key still lacks the product: fail loudly.
    assert res.returncode != 0 and "Finding an approved atat-product consumer key" in res.stderr
    assert any(c.endswith("/apps/atat-demo-app/keys/oldkey") for c in mutating(env))
    env["STUB_RULES"] = json.dumps([
        {"method": "POST", "match": "/developers/atat-dev@example.com/apps", "code": 409, "body": {}},
        {"method": "GET", "match": "/apps/atat-demo-app", "code": 200, "body": app_with},
    ])
    res = run(DEPLOY, env)
    assert res.returncode == 0, res.stderr
    assert (Path(env["ATA_STACK_DIR"]) / ".atat-api-key").read_text() == "oldkey\n"


def test_bearer_token_is_in_a_curl_config_never_on_argv(env):
    """sec L2: `ps` must not show the OAuth token."""
    res = run(DEPLOY, env)
    assert res.returncode == 0, res.stderr
    argv = Path(env["STUB_CALLS"] + ".argv").read_text()
    assert argv and "stub-token" not in argv and "Authorization" not in argv
    configs = Path(env["STUB_CALLS"] + ".config").read_text().splitlines()
    assert configs and set(configs) == {'header = "Authorization: Bearer stub-token"'}


def test_credential_bearing_failures_withhold_body_and_key(env):
    """sec L1: a failed read of the app (body = every consumerKey/Secret) or of its key URL
    prints neither."""
    secret_app = {"name": "atat-demo-app", "credentials": [
        {"consumerKey": "oldkey-SECRET", "consumerSecret": "csecret-SECRET", "status": "approved", "apiProducts": []}]}
    env["STUB_RULES"] = json.dumps([
        {"method": "POST", "match": "/developers/atat-dev@example.com/apps", "code": 409, "body": {}},
        {"method": "GET", "match": "/apps/atat-demo-app", "code": 500, "body": secret_app},
    ])
    res = run(DEPLOY, env)
    assert res.returncode != 0
    assert "Reading app atat-demo-app failed: HTTP 500" in res.stderr and "body withheld" in res.stderr
    assert "SECRET" not in res.stderr + res.stdout
    env["STUB_RULES"] = json.dumps([
        {"method": "POST", "match": "/apps/atat-demo-app/keys/oldkey-SECRET", "code": 403,
         "body": {"error": {"message": "denied for oldkey-SECRET"}}},
        {"method": "POST", "match": "/developers/atat-dev@example.com/apps", "code": 409, "body": {}},
        {"method": "GET", "match": "/apps/atat-demo-app", "code": 200, "body": secret_app},
    ])
    res = run(DEPLOY, env)
    assert res.returncode != 0
    assert "/apps/atat-demo-app/keys/<withheld>" in res.stderr
    assert "SECRET" not in res.stderr + res.stdout


def test_deploy_never_uses_a_key_from_another_app(env):
    other = {"name": "ata-demo-app", "credentials": [
        {"consumerKey": "livekey", "status": "approved", "apiProducts": [{"apiproduct": "atat-product"}]}]}
    env["STUB_RULES"] = json.dumps([{"method": "POST", "match": "/apps", "code": 201, "body": other}])
    res = run(DEPLOY, env)
    assert res.returncode != 0
    assert not (Path(env["ATA_STACK_DIR"]) / ".atat-api-key").exists()


# ---------------------------------------------------------------- the prefix guard in the shared lib


@pytest.mark.parametrize("url,ok", [
    ("https://apigee.googleapis.com/v1/organizations/org-x/apis/atat-orders-v1", True),
    ("https://apigee.googleapis.com/v1/organizations/org-x/apis?name=atat-orders-v1&action=import", True),
    ("https://apigee.googleapis.com/v1/organizations/org-x/environments/env-x/targetservers", True),
    ("https://apigee.googleapis.com/v1/organizations/org-x/developers/atat-dev@example.com/apps/atat-demo-app", True),
    ("https://apigee.googleapis.com/v1/organizations/org-x/apis/ata-orders-v1", False),
    ("https://apigee.googleapis.com/v1/organizations/org-x/apis?name=ata-orders-v1&action=import", False),
    ("https://apigee.googleapis.com/v1/organizations/org-x/environments/env-x/targetservers/ata-orders-target", False),
    ("https://apigee.googleapis.com/v1/organizations/org-x/developers/ata-dev@example.com/apps/atat-demo-app", False),
    ("https://apigee.googleapis.com/v1/organizations/org-x/developers/atat-dev@example.com/apps/ata-demo-app", False),
    ("https://apigee.googleapis.com/v1/organizations/other/apis/atat-orders-v1", False),
    ("https://apigee.googleapis.com/v1/organizations/org-x/apis/atat-", False),
    ("https://apigee.googleapis.com/v1/organizations/org-x/apiproducts/atatx-product", False),
])
def test_guard_url(url, ok):
    res = subprocess.run([TEST_BASH, "-c", f'set -euo pipefail; . "{BUNDLES}/apigee-lib.sh"; '
                          f'NAME_PREFIX=atat; APIGEE_ORG=org-x; apigee_guard_url "$1"', "x", url],
                         capture_output=True, text=True, check=False)
    assert (res.returncode == 0) == ok, res.stderr
    if not ok:
        assert "SAFETY GUARD" in res.stderr


# ---------------------------------------------------------------- teardown


@pytest.mark.parametrize("var", ["NAME_PREFIX", "APIGEE_ORG", "APIGEE_ENV"])
def test_teardown_requires_each_input(env, var):
    del env[var]
    res = run(TEARDOWN, env, "--apply", "--yes")
    assert res.returncode != 0 and f"{var} is required" in res.stderr
    assert not Path(env["STUB_CALLS"]).exists()


def test_teardown_dry_run_by_default_is_read_only(env):
    res = run(TEARDOWN, env)
    assert res.returncode == 0, res.stderr
    assert "DRY RUN" in res.stdout and "would DELETE" in res.stdout
    assert mutating(env) == []
    assert all("/ata-" not in c for c in calls(env))


def test_teardown_apply_needs_confirmation(env):
    res = run(TEARDOWN, env, "--apply", stdin="ata\n")
    assert res.returncode != 0 and "confirmation did not match" in res.stderr
    assert not Path(env["STUB_CALLS"]).exists()
    res = run(TEARDOWN, env, "--apply", stdin="")
    assert res.returncode != 0 and mutating(env) == []


@pytest.mark.parametrize("how", [("--yes",), ("typed",)])
def test_teardown_apply_deletes_only_prefixed_resources(env, how):
    args = ["--apply"] + (["--yes"] if how == ("--yes",) else [])
    env["STUB_LIST"] = json.dumps({
        "apis": ["ata-orders-v1", "httpbin", "atat-orders-v1", "atat-inventory-v1"],
        "apiproducts": ["ata-product", "atat-product"],
        "developers": ["ata-dev@example.com", "atat-dev@example.com"],
        "targetservers": ["ata-orders-target", "atat-orders-target", "atat-inventory-target"]})
    res = run(TEARDOWN, env, *args, stdin="atat\n")
    assert res.returncode == 0, res.stdout + res.stderr
    base = "https://apigee.googleapis.com/v1/organizations/org-x"
    assert mutating(env) == [
        f"DELETE {base}/environments/env-x/apis/atat-orders-v1/revisions/3/deployments",
        f"DELETE {base}/apis/atat-orders-v1",
        f"DELETE {base}/environments/env-x/apis/atat-inventory-v1/revisions/3/deployments",
        f"DELETE {base}/apis/atat-inventory-v1",
        f"DELETE {base}/developers/atat-dev@example.com/apps/atat-demo-app",
        f"DELETE {base}/developers/atat-dev@example.com",
        f"DELETE {base}/apiproducts/atat-product",
        f"DELETE {base}/environments/env-x/targetservers/atat-orders-target",
        f"DELETE {base}/environments/env-x/targetservers/atat-inventory-target",
    ]
    assert all("/ata-" not in c for c in calls(env))
    assert "Teardown complete" in res.stdout
    for coll in ("/apis", "/apiproducts", "/developers", "/environments/env-x/targetservers"):
        assert f"GET {base}{coll}" in calls(env)


def test_teardown_treats_404_as_gone(env):
    env["STUB_ABSENT"] = "1"
    res = run(TEARDOWN, env, "--apply", "--yes")
    assert res.returncode == 0, res.stderr
    assert "already gone" in res.stdout and "Teardown complete" in res.stdout


def test_teardown_fails_on_other_errors_with_body(env):
    env["STUB_RULES"] = json.dumps([{"method": "DELETE", "match": "/apis/atat-orders-v1", "code": 400,
                                     "body": {"error": {"message": "proxy still deployed in env other"}}}])
    res = run(TEARDOWN, env, "--apply", "--yes")
    assert res.returncode != 0 and "proxy still deployed in env other" in res.stderr


def test_teardown_verification_fails_if_prefixed_resources_remain(env):
    env["STUB_RULES"] = json.dumps([{"method": "GET", "match": "/apiproducts", "code": 200,
                                     "body": {"apiProduct": [{"name": "atat-leftover"}, {"name": "ata-product"}]}}])
    res = run(TEARDOWN, env, "--apply", "--yes")
    assert res.returncode != 0 and "atat-leftover" in res.stderr
    assert "ata-product" not in res.stderr


# ---------------------------------------------------------------- portability of the demo/apigee helpers


def _gnu_only_patterns():
    spec = importlib.util.spec_from_file_location("macos_userland", Path(__file__).parent / "test_macos_userland.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.GNU_ONLY


@pytest.mark.parametrize("path", sorted(BUNDLES.glob("*.sh")) + [DEPLOY, TEARDOWN], ids=lambda p: p.name)
def test_helpers_are_portable(path):
    res = subprocess.run([TEST_BASH, "-n", str(path)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    code = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]
    hits = [ln for ln in code for rx in _gnu_only_patterns().values() if rx.search(ln)]
    assert not hits, hits
    text = "\n".join(code)
    for bad in ("declare -A", "local -A", "mapfile", "readarray", ",,}", "^^}", "\\s", "sed -i"):
        assert bad not in text, (path.name, bad)


def test_no_hardcoded_environment_in_scripts():
    for path in [DEPLOY, TEARDOWN, *BUNDLES.glob("*.sh")]:
        text = path.read_text()
        for literal in ("cloud-assist-fde-2", "nip.io", "4klvlqnmpa", "us-west1", "ata-", "/ata/", '"prod"', "=prod"):
            assert literal not in text, (path.name, literal)
