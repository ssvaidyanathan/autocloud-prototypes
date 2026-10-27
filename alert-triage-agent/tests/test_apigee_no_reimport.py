"""deploy-apigee-proxies.sh imports and deploys a proxy only when its rendered bundle differs
from the revision deployed in APIGEE_ENV (fresh-noreimport).

Every reset-scenarios.sh run used to import and deploy a new revision of both proxies. Those
deployments landed in the agent's change window (a later ticket could blame an Apigee
"deployment" that changed nothing) and burned revisions. Now an unchanged proxy is a logged
no-op, any doubt (read failure, not deployed, several deployments, wrong SA, not READY,
unreadable bundle) falls back to import + deploy with a WARNING, and TargetServers, product
and developer are not rewritten when they already hold the wanted values.

The curl stub here is a stateful fake of the Apigee management API: imported bundles are kept
as zips, so the bundle download returns what was imported. As real Apigee does, the download
rewrites the root descriptor apiproxy/<proxy>.xml (revision attribute, timestamps, BasePaths,
Policies order, no XML declaration); that rewrite was observed on the live org (read-only GET
of ata-orders-v1 r183, 2026-10-27) and must not count as a change.

Run with a real bash 3.2: ATA_TEST_BASH=/path/to/bash-3.2.57/bash pytest tests/test_apigee_no_reimport.py
"""

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
RENDER = ROOT / "demo" / "apigee" / "render-bundle.sh"
LIB = ROOT / "demo" / "apigee" / "apigee-lib.sh"
DEPLOY = ROOT / "scripts" / "deploy-apigee-proxies.sh"
RESET = ROOT / "scripts" / "reset-scenarios.sh"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

P = "atat"
BASE = "https://apigee.googleapis.com/v1/organizations/org-x"
ENV_URL = f"{BASE}/environments/env-x"
SA = f"{P}-apigee-sa@proj-x.iam.gserviceaccount.com"
SA_REF = f"projects/-/serviceAccounts/{SA}"
PROXIES = [f"{P}-orders-v1", f"{P}-inventory-v1"]

FAKE_APIGEE = r'''#!/usr/bin/env python3
import json, os, re, shutil, sys, zipfile
args = sys.argv[1:]
method, out, data, url, upload = "GET", None, "", None, None
i = 0
while i < len(args):
    a = args[i]
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "-o": out = args[i + 1]; i += 2; continue
    if a == "-F": upload = args[i + 1].split("@", 1)[1]; i += 2; continue
    if a == "-d": data = args[i + 1]; i += 2; continue
    if a in ("-w", "-H", "--config"): i += 2; continue
    if a.startswith("-"): i += 1; continue
    url = a; i += 1
with open(os.environ["STUB_CALLS"], "a") as f:
    f.write(f"{method} {url}\n")
sdir = os.environ["STUB_DIR"]
state_file = os.path.join(sdir, "apigee-state.json")
st = json.load(open(state_file)) if os.path.exists(state_file) else {}
for k in ("revisions", "deployments", "targetservers", "apiproducts", "developers", "apps"):
    st.setdefault(k, {})
path, _, query = url.partition("?")
code, body, raw = 200, {}, None

def rule():
    for r in json.loads(os.environ.get("STUB_RULES", "[]")):
        if r["method"] == method and r["match"] in url + "?":
            return r
    return None

def exported(proxy, rev):
    """The stored zip with its root descriptor rewritten the way Apigee exports it."""
    src = st["revisions"][proxy][rev]
    dst = os.path.join(sdir, f"export-{proxy}-{rev}.zip")
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            content = zin.read(info)
            if info.filename == f"apiproxy/{proxy}.xml":
                text = content.decode()
                text = re.sub(r"<\?xml[^>]*\?>\s*", "", text)
                text = re.sub(r'revision="\d+"', f'revision="{rev}"', text)
                text = re.sub(r"\s*<(CreatedBy|LastModifiedBy|ManifestVersion)>[^<]*</\1>", "", text)
                text = re.sub(r"\s*<ConfigurationVersion[^>]*/>", "", text)
                text = re.sub(r"<(CreatedAt|LastModifiedAt)>\d+<", r"<\1>1793142055354<", text)
                text = text.replace("</Description>", "</Description>\n    <BasePaths>/x</BasePaths>")
                pol = re.search(r"<Policies>(.*?)</Policies>", text, re.S)
                if pol:
                    items = sorted(re.findall(r"<Policy>[^<]*</Policy>", pol.group(1)))
                    text = text.replace(pol.group(0), "<Policies>\n        " + "\n        ".join(items) + "\n    </Policies>")
                content = text.rstrip("\n").encode()
            zout.writestr(info.filename, content)
    return dst

def crud(coll, key, store_body):
    global code, body
    m = re.search(r"/" + coll + r"(?:/([^/]+))?$", path)
    name = m.group(1)
    items = st[coll]
    if method == "GET":
        if name in items: body = items[name]
        else: code, body = 404, {"error": {"code": 404}}
    elif method == "POST" and name is None:
        d = json.loads(data)
        if d[key] in items: code, body = 409, {"error": {"code": 409}}
        else: items[d[key]] = store_body(d); code, body = 201, items[d[key]]
    elif method == "PUT":
        items[name] = store_body(json.loads(data)); body = items[name]

r = rule()
if r is not None:
    code, body = r["code"], r.get("body", {})
elif method == "GET" and path == os.environ["STUB_ENV_URL"]:
    body = {"name": "env-x"}
elif re.search(r"/environments/[^/]+/apis/([^/]+)/deployments$", path) and method == "GET":
    proxy = re.search(r"/apis/([^/]+)/deployments$", path).group(1)
    if proxy not in st["revisions"]: code, body = 404, {"error": {"code": 404}}
    else:
        deps = st["deployments"].get(proxy, [])
        body = {"deployments": [dict(d, environment="env-x", apiProxy=proxy) for d in deps]} if deps else {}
elif re.search(r"/apis/([^/]+)/revisions/(\d+)/deployments$", path):
    proxy, rev = re.search(r"/apis/([^/]+)/revisions/(\d+)/deployments$", path).groups()
    if method == "POST":
        sa = re.search(r"serviceAccount=([^&]+)", query)
        st["deployments"][proxy] = [{"revision": rev, "serviceAccount": sa.group(1) if sa else None}]
        body = {"revision": rev}
    elif any(d["revision"] == rev for d in st["deployments"].get(proxy, [])):
        body = {"revision": rev, "state": os.environ.get("STUB_DEPLOY_STATE", "READY")}
    else:
        code, body = 404, {"error": {"code": 404}}
elif re.search(r"/apis/([^/]+)/revisions/(\d+)$", path) and method == "GET" and query == "format=bundle":
    proxy, rev = re.search(r"/apis/([^/]+)/revisions/(\d+)$", path).groups()
    if rev in st["revisions"].get(proxy, {}): raw = exported(proxy, rev)
    else: code, body = 404, {"error": {"code": 404}}
elif path.endswith("/apis") and method == "POST" and "action=import" in query:
    proxy = re.search(r"name=([^&]+)", query).group(1)
    revs = st["revisions"].setdefault(proxy, {})
    rev = str(max([int(x) for x in revs] or [0]) + 1)
    keep = os.path.join(sdir, f"stored-{proxy}-{rev}.zip")
    shutil.copy(upload, keep)
    revs[rev] = keep
    body = {"name": proxy, "revision": rev}
elif re.search(r"/targetservers(/[^/]+)?$", path):
    crud("targetservers", "name", lambda d: dict(d, protocol="HTTP"))
elif re.search(r"/apiproducts(/[^/]+)?$", path):
    crud("apiproducts", "name", lambda d: dict(d, createdAt="1", lastModifiedAt="2"))
elif re.search(r"/developers/[^/]+/apps(/[^/]+)?$", path):
    m = re.search(r"/apps(?:/([^/]+))?$", path)
    if method == "POST" and m.group(1) is None:
        d = json.loads(data)
        if d["name"] in st["apps"]: code, body = 409, {}
        else:
            st["apps"][d["name"]] = {"name": d["name"], "credentials": [{"consumerKey": "key-" + d["name"], "status": "approved",
                                     "apiProducts": [{"apiproduct": p} for p in d["apiProducts"]]}]}
            code, body = 201, st["apps"][d["name"]]
    elif method == "GET" and m.group(1) in st["apps"]:
        body = st["apps"][m.group(1)]
    else:
        code, body = 404, {}
elif re.search(r"/developers(/[^/]+)?$", path):
    crud("developers", "email", lambda d: dict(d, developerId="dev-1", status="active", apps=[],
                                               organizationName="org-x", createdAt="1", lastModifiedAt="2"))
json.dump(st, open(state_file, "w"))
if out:
    if raw: shutil.copy(raw, out)
    else: open(out, "w").write(json.dumps(body))
sys.stdout.write(str(code))
'''

GCLOUD_STUB = r'''#!/bin/sh
echo "gcloud $*" >> "$STUB_CALLS"
case "$1 $2" in
  "auth print-access-token") echo "stub-token" ;;
  "run services") echo "https://$4-abc123-uw.a.run.app" ;;
esac
'''


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bash").symlink_to(TEST_BASH)
    for name, body in [("curl", FAKE_APIGEE), ("gcloud", GCLOUD_STUB)]:
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    stack = tmp_path / "stack"
    stack.mkdir()
    keep = {k: v for k, v in os.environ.items()
            if k not in ("PROJECT_ID", "REGION", "NAME_PREFIX", "APIGEE_ORG", "APIGEE_ENV", "APIGEE_HOST",
                         "STUB_RULES", "ATA_API_KEY", "MOCK_MODE", "DRY_RUN")}
    keep.update(PATH=f"{bin_dir}{os.pathsep}{keep['PATH']}", STUB_CALLS=str(tmp_path / "calls"),
                STUB_DIR=str(tmp_path), STUB_ENV_URL=ENV_URL, TMPDIR=str(tmp_path),
                PROJECT_ID="proj-x", REGION="region-x", NAME_PREFIX=P, APIGEE_ORG="org-x",
                APIGEE_ENV="env-x", APIGEE_HOST="host.example.test", ATA_STACK_DIR=str(stack),
                DEPLOY_POLL_SECS="0", DEPLOY_TIMEOUT_SECS="3")
    return keep


def deploy(env, **extra):
    return subprocess.run([TEST_BASH, str(DEPLOY)], env=dict(env, **extra), capture_output=True, text=True,
                          timeout=120, check=False)


def calls(env):
    p = Path(env["STUB_CALLS"])
    lines = p.read_text().splitlines() if p.exists() else []
    Path(env["STUB_CALLS"]).write_text("")  # each call() returns the calls since the last one
    return [c for c in lines if not c.startswith("gcloud ")]


def writes(cs):
    return [c for c in cs if c.split(" ", 1)[0] in ("POST", "PUT", "DELETE")]


def imports(cs):
    return [c for c in cs if "action=import" in c]


def deploys(cs):
    return [c for c in cs if c.startswith("POST ") and "/deployments?" in c]


def state(env):
    return json.loads((Path(env["STUB_DIR"]) / "apigee-state.json").read_text())


def set_state(env, st):
    (Path(env["STUB_DIR"]) / "apigee-state.json").write_text(json.dumps(st))


def strict_s3_zip(tmp_path):
    """The bundle S3 imports: the orders render with SA-SpikeArrest made strict (1pm)."""
    out = tmp_path / "s3-render"
    subprocess.run([TEST_BASH, str(RENDER), "orders-v1", P, str(out)], check=True, capture_output=True, text=True)
    (out / "apiproxy" / "policies" / "SA-SpikeArrest.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<SpikeArrest continueOnError="false" enabled="true" name="SA-SpikeArrest">\n'
        '    <DisplayName>Spike Arrest Rate Limiting (Strict 1pm)</DisplayName>\n'
        '    <Rate>1pm</Rate>\n</SpikeArrest>\n')
    zip_path = tmp_path / "s3-strict.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(out).as_posix())
    return zip_path


def inject_s3_fault(env, tmp_path):
    """What S3 does to Apigee: import the strict bundle as a new revision and deploy it."""
    st = state(env)
    proxy = f"{P}-orders-v1"
    rev = str(max(int(r) for r in st["revisions"][proxy]) + 1)
    keep = tmp_path / f"stored-{proxy}-{rev}.zip"
    shutil.copy(strict_s3_zip(tmp_path), keep)
    st["revisions"][proxy][rev] = str(keep)
    st["deployments"][proxy] = [{"revision": rev, "serviceAccount": SA_REF}]
    set_state(env, st)
    return rev


@pytest.fixture
def deployed(env):
    """A stack after a first, fresh deploy (r1 of both proxies deployed)."""
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    cs = calls(env)
    assert len(imports(cs)) == 2 and len(deploys(cs)) == 2
    return env


# ---------------------------------------------------------------- the decision


def test_unchanged_proxies_are_not_imported_or_deployed(deployed):
    env = deployed
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    cs = calls(env)
    assert imports(cs) == [] and deploys(cs) == []
    for proxy in PROXIES:
        assert f"{proxy}: deployed revision 1 matches the rendered bundle; not redeployed." in res.stdout
        assert list(state(env)["revisions"][proxy]) == ["1"]
    # The comparison used the deployed revision's bundle.
    assert f"GET {BASE}/apis/{P}-orders-v1/revisions/1?format=bundle" in cs
    assert "WARNING" not in res.stdout + res.stderr


def test_unchanged_stack_writes_nothing_at_all(deployed):
    """TargetServers, product and developer already hold the wanted values: no PUT/POST
    (the app's POST answers 409 "exists" and changes nothing)."""
    env = deployed
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert writes(calls(env)) == [f"POST {BASE}/developers/{P}-dev@example.com/apps"]
    for what in [f"TargetServer {P}-orders-target", f"TargetServer {P}-inventory-target",
                 f"API product {P}-product", f"Developer {P}-dev@example.com"]:
        assert f"{what} unchanged; not updated." in res.stdout
    assert (Path(env["ATA_STACK_DIR"]) / f".{P}-api-key").read_text() == f"key-{P}-demo-app\n"


def test_changed_targetserver_host_is_updated(deployed):
    env = deployed
    st = state(env)
    st["targetservers"][f"{P}-orders-target"]["host"] = "old-host.a.run.app"
    set_state(env, st)
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    ws = writes(calls(env))
    assert f"PUT {ENV_URL}/targetservers/{P}-orders-target" in ws
    assert f"PUT {ENV_URL}/targetservers/{P}-inventory-target" not in ws
    assert state(env)["targetservers"][f"{P}-orders-target"]["host"] == f"{P}-orders-api-abc123-uw.a.run.app"


@pytest.mark.parametrize("coll,name,url,extra", [
    ("apiproducts", f"{P}-product", f"{BASE}/apiproducts/{P}-product",
     {"quota": "1", "quotaInterval": "1", "quotaTimeUnit": "minute"}),
    ("apiproducts", f"{P}-product", f"{BASE}/apiproducts/{P}-product",
     {"attributes": [{"name": "access", "value": "private"}]}),
    ("targetservers", f"{P}-orders-target", f"{ENV_URL}/targetservers/{P}-orders-target",
     {"sSLInfo": {"enabled": True, "ignoreValidationErrors": True}}),
    ("targetservers", f"{P}-orders-target", f"{ENV_URL}/targetservers/{P}-orders-target",
     {"protocol": "GRPC"}),
    ("developers", f"{P}-dev@example.com", f"{BASE}/developers/{P}-dev@example.com",
     {"attributes": [{"name": "tier", "value": "x"}]}),
])
def test_extra_field_drift_is_cleared_by_a_full_replace_put(deployed, coll, name, url, extra):
    """F1: a resource holding a superset of the wanted fields (e.g. a hand-added product
    quota) is not "unchanged": the PUT full replace runs and the drift is gone."""
    env = deployed
    st = state(env)
    st[coll][name].update(extra)
    set_state(env, st)
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    ws = writes(calls(env))
    assert f"PUT {url}" in ws
    assert [w for w in ws if w.startswith("PUT ")] == [f"PUT {url}"]
    have = state(env)[coll][name]
    for k, v in extra.items():
        assert have.get(k) != v
    assert "unchanged; not updated." in res.stdout  # the untouched resources still skip


def test_only_server_set_fields_differing_writes_nothing(deployed):
    """Server-set fields (timestamps, developerId, status, apps, ...) and the TargetServer's
    default protocol never force a write, whatever their values."""
    env = deployed
    st = state(env)
    st["apiproducts"][f"{P}-product"].update(createdAt="1792708869081", lastModifiedAt="1793142176595")
    st["developers"][f"{P}-dev@example.com"].update(
        developerId="c7f7d1cb", organizationName="org-y", status="inactive", apps=[f"{P}-demo-app"],
        createdAt="1792708869395", lastModifiedAt="1793142177034")
    for ts in st["targetservers"].values():
        assert ts["protocol"] == "HTTP"
    set_state(env, st)
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert writes(calls(env)) == [f"POST {BASE}/developers/{P}-dev@example.com/apps"]
    for what in [f"TargetServer {P}-orders-target", f"TargetServer {P}-inventory-target",
                 f"API product {P}-product", f"Developer {P}-dev@example.com"]:
        assert f"{what} unchanged; not updated." in res.stdout


def test_after_the_s3_fault_the_proxy_is_imported_and_deployed(deployed, tmp_path):
    env = deployed
    strict = inject_s3_fault(env, tmp_path)
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    cs = calls(env)
    assert imports(cs) == [f"POST {BASE}/apis?name={P}-orders-v1&action=import"]
    new = str(int(strict) + 1)
    assert deploys(cs) == [f"POST {ENV_URL}/apis/{P}-orders-v1/revisions/{new}/deployments"
                           f"?override=true&serviceAccount={SA_REF}"]
    assert (f"{P}-orders-v1: deployed revision {strict} differs from the rendered bundle "
            f"(apiproxy/policies/SA-SpikeArrest.xml differs).") in res.stdout
    assert f"{P}-inventory-v1: deployed revision 1 matches the rendered bundle; not redeployed." in res.stdout
    assert state(env)["deployments"][f"{P}-orders-v1"] == [{"revision": new, "serviceAccount": SA_REF}]
    # And the restore is itself the new baseline: the next run is a no-op.
    res = deploy(env)
    assert res.returncode == 0
    assert imports(calls(env)) == []


@pytest.mark.parametrize("rule,why", [
    ({"method": "GET", "match": "/apis/atat-orders-v1/deployments?", "code": 500}, "could not read its deployments in env-x (HTTP 500)"),
    ({"method": "GET", "match": "/apis/atat-orders-v1/deployments?", "code": 200, "body": "not json"}, "unreadable deployments response"),
    ({"method": "GET", "match": "/apis/atat-orders-v1/revisions/1/deployments", "code": 503}, "is not READY (HTTP 503"),
    ({"method": "GET", "match": "/apis/atat-orders-v1/revisions/1?format=bundle", "code": 403}, "could not download revision 1 (HTTP 403)"),
    ({"method": "GET", "match": "/apis/atat-orders-v1/revisions/1?format=bundle", "code": 200, "body": {"x": 1}}, "could not compare revision 1"),
])
def test_any_failure_to_decide_imports_and_deploys_with_a_warning(deployed, rule, why):
    env = deployed
    res = deploy(env, STUB_RULES=json.dumps([rule]))
    assert res.returncode == 0, res.stdout + res.stderr
    cs = calls(env)
    assert imports(cs) == [f"POST {BASE}/apis?name={P}-orders-v1&action=import"]
    assert deploys(cs) == [f"POST {ENV_URL}/apis/{P}-orders-v1/revisions/2/deployments"
                           f"?override=true&serviceAccount={SA_REF}"]
    assert f"WARNING: {P}-orders-v1: " in res.stderr and why in res.stderr, res.stderr
    assert "importing and deploying the rendered bundle." in res.stderr


def test_not_deployed_imports_and_deploys_with_a_warning(deployed):
    env = deployed
    st = state(env)
    st["deployments"][f"{P}-inventory-v1"] = []
    set_state(env, st)
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    cs = calls(env)
    assert imports(cs) == [f"POST {BASE}/apis?name={P}-inventory-v1&action=import"]
    assert len(deploys(cs)) == 1
    assert f"WARNING: {P}-inventory-v1: not deployed in env-x" in res.stderr


def test_fresh_stack_imports_both_with_a_not_found_warning(env):
    res = deploy(env)
    assert res.returncode == 0, res.stdout + res.stderr
    cs = calls(env)
    assert len(imports(cs)) == 2 and len(deploys(cs)) == 2
    for proxy in PROXIES:
        assert f"WARNING: {proxy}: not deployed in env-x (HTTP 404: no such proxy yet)" in res.stderr


def test_multiple_deployments_import_and_deploy_with_a_warning(deployed):
    env = deployed
    st = state(env)
    st["deployments"][f"{P}-orders-v1"].append({"revision": "1", "serviceAccount": SA_REF})
    set_state(env, st)
    res = deploy(env)
    assert res.returncode == 0
    assert imports(calls(env)) == [f"POST {BASE}/apis?name={P}-orders-v1&action=import"]
    assert f"WARNING: {P}-orders-v1: 2 deployments in env-x" in res.stderr


@pytest.mark.parametrize("sa", [None, "projects/-/serviceAccounts/other@proj-x.iam.gserviceaccount.com"])
def test_deployment_under_another_service_account_is_redeployed(deployed, sa):
    env = deployed
    st = state(env)
    st["deployments"][f"{P}-orders-v1"][0]["serviceAccount"] = sa
    set_state(env, st)
    res = deploy(env)
    assert res.returncode == 0
    assert imports(calls(env)) == [f"POST {BASE}/apis?name={P}-orders-v1&action=import"]
    assert f"WARNING: {P}-orders-v1: deployed revision 1 runs as" in res.stderr


def test_not_ready_deployment_is_redeployed(deployed):
    env = deployed
    res = deploy(env, STUB_DEPLOY_STATE="ERROR")
    # Not READY -> import + deploy; the new deployment's poll then sees ERROR and fails loudly.
    assert res.returncode != 0
    assert "is not READY" in res.stderr and "state ERROR" in res.stderr
    assert len(imports(calls(env))) == 1


def test_prefix_guard_still_holds_for_every_call(deployed):
    env = deployed
    res = deploy(env)
    assert res.returncode == 0
    for c in calls(env):
        url = c.split(" ", 1)[1]
        assert url.startswith(BASE + "/"), c
        assert "/ata-" not in url and "/ata/" not in url, c
    # The new GET of a revision bundle goes through the guard too.
    lib = f'. "{LIB}"; NAME_PREFIX={P}; APIGEE_ORG=org-x; APIGEE_TOKEN=t; '
    for url in [f"{BASE}/apis/ata-orders-v1/revisions/1?format=bundle",
                f"https://apigee.googleapis.com/v1/organizations/other/apis/{P}-orders-v1/revisions/1?format=bundle"]:
        res = subprocess.run([TEST_BASH, "-c", lib + f'apigee_download "{url}" "{env["STUB_DIR"]}/x.zip"'],
                             env=env, capture_output=True, text=True, check=False)
        assert res.returncode != 0 and "SAFETY GUARD" in res.stderr
    assert calls(env) == []


def test_bundle_compare_ignores_what_apigee_regenerates_but_not_content(tmp_path):
    """Live export of r183 vs the render: identical except the root descriptor's generated
    metadata. A changed Description (labels) or policy list is a change."""
    def render_zip(name, edit=None):
        out = tmp_path / name
        subprocess.run([TEST_BASH, str(RENDER), "orders-v1", P, str(out), str(tmp_path / f"{name}.zip")],
                       check=True, capture_output=True, text=True)
        if edit:
            root = out / "apiproxy" / f"{P}-orders-v1.xml"
            root.write_text(edit(root.read_text()))
            (tmp_path / f"{name}.zip").unlink()
            with zipfile.ZipFile(tmp_path / f"{name}.zip", "w") as zf:
                for f in sorted(out.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(out).as_posix())
        return tmp_path / f"{name}.zip"

    def cmp(a, b):
        return subprocess.run([TEST_BASH, "-c", f'. "{LIB}"; apigee_bundles_equal "$1" "$2"', "_", str(a), str(b)],
                              capture_output=True, text=True, check=False)
    base = render_zip("base")
    regenerated = render_zip("regen", lambda t: t.replace('revision="1"', 'revision="183"')
                             .replace("<CreatedAt>1792708000000</CreatedAt>", "<CreatedAt>1793142055354</CreatedAt>")
                             .replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n', "")
                             .replace("<Policy>RF-BlockAdmin</Policy>", "").replace(
                                 "<Policy>SA-SpikeArrest</Policy>", "<Policy>SA-SpikeArrest</Policy><Policy>RF-BlockAdmin</Policy>"))
    assert cmp(base, regenerated).returncode == 0
    relabeled = render_zip("label", lambda t: t.replace("owner=sre-prototypes", "owner=someone"))
    res = cmp(base, relabeled)
    assert res.returncode == 1 and f"apiproxy/{P}-orders-v1.xml differs" in res.stdout
    dropped = render_zip("drop", lambda t: t.replace("<Policy>RF-BlockAdmin</Policy>", ""))
    assert cmp(base, dropped).returncode == 1
    assert cmp(base, strict_s3_zip(tmp_path)).returncode == 1
    (tmp_path / "junk.zip").write_text("{}")
    res = cmp(base, tmp_path / "junk.zip")
    assert res.returncode == 2 and "unreadable bundle" in res.stdout


# ---------------------------------------------------------------- reset, end to end (step 4)


def reset_step4(env):
    """reset-scenarios.sh step 4 (restore_apigee_proxies) against the fake, by sourcing the
    reset as the other reset tests do; it runs the real deploy-apigee-proxies.sh."""
    script = Path(env["STUB_DIR"]) / "reset4.sh"
    script.write_text(f'source "{RESET}"\nrestore_apigee_proxies\n')
    pin = Path(env["STUB_DIR"]) / f"inventory-pin.{P}.env"
    pin.write_text(f"PIN_INVENTORY_REVISION={P}-inventory-api-00001-abc\nPIN_INVENTORY_DIGEST=sha256:{'ab' * 32}\n"
                   "PIN_INVENTORY_PREVIOUS_REVISION=\nPIN_INVENTORY_PREVIOUS_DIGEST=\n")
    return subprocess.run([TEST_BASH, str(script)], env=dict(env, MOCK_MODE="false", DRY_RUN="false",
                                                             ATA_INVENTORY_PIN_FILE=str(pin)),
                          capture_output=True, text=True, timeout=120, check=False)


def test_reset_with_nothing_changed_adds_no_revision_and_no_deployment(deployed, tmp_path):
    env = deployed
    before = state(env)
    for _ in range(2):
        res = reset_step4(env)
        assert res.returncode == 0, res.stdout + res.stderr
        cs = calls(env)
        assert imports(cs) == [] and deploys(cs) == []
        for proxy in PROXIES:
            assert f"{proxy}: deployed revision 1 matches the rendered bundle; not redeployed." in res.stdout
        assert "Apigee proxy baselines restored." in res.stdout
    after = state(env)
    assert after["revisions"] == before["revisions"] and after["deployments"] == before["deployments"]

    # After S3's fault, the reset restores the baseline (one import + deploy), then is quiet again.
    strict = inject_s3_fault(env, tmp_path)
    res = reset_step4(env)
    assert res.returncode == 0, res.stdout + res.stderr
    cs = calls(env)
    assert imports(cs) == [f"POST {BASE}/apis?name={P}-orders-v1&action=import"] and len(deploys(cs)) == 1
    assert f"deployed revision {strict} differs from the rendered bundle" in res.stdout
    assert "Imported revision" in res.stdout
    res = reset_step4(env)
    assert res.returncode == 0
    assert imports(calls(env)) == []


def test_reset_surfaces_the_warning_that_forced_a_redeploy(deployed):
    env = deployed
    env = dict(env, STUB_RULES=json.dumps([{"method": "GET", "match": "/apis/atat-orders-v1/deployments?", "code": 500}]))
    res = reset_step4(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[WARN]" in res.stderr and f"WARNING: {P}-orders-v1: could not read its deployments" in res.stderr


def test_reset_fails_loudly_when_the_deploy_fails(deployed):
    env = dict(deployed, STUB_RULES=json.dumps([{"method": "POST", "match": "action=import", "code": 500}]))
    st = state(env)
    st["deployments"][f"{P}-orders-v1"] = []
    set_state(env, st)
    res = reset_step4(env)
    assert res.returncode != 0
    assert "FATAL: deploy-apigee-proxies.sh failed" in res.stderr
