"""From-scratch deploy scripts: deploy-stage1 phases, build-image, set-secrets, verify-stage1,
teardown-stack and the bridge's VERIFIER_SA validation.

Every script runs from a tmp copy of the tree against stub gcloud/terraform/curl/psql/
cloud-sql-proxy (Python or sh) that append each call to one shared log, so the tests can
check the order of calls across scripts. Nothing here reaches a real cloud API.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))
PROJECT_NUMBER = "123456789012"
ROOTS = ["foundation", "database", "applications", "observability", "triage-iam"]

STUB_GCLOUD = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
args = sys.argv[1:]
d = os.environ["STUB_DIR"]
with open(os.path.join(d, "log"), "a") as f:
    f.write(json.dumps(["gcloud"] + args) + "\n")
def opt(name):
    for a in args:
        if a.startswith("--" + name + "="):
            return a.split("=", 1)[1]
    return None
deployed = os.environ.get("STUB_DEPLOYED", "").split()
def lines(name):
    return [l for l in os.environ.get(name, "").split(",") if l]
if args[:2] == ["auth", "list"]:
    print("op@example.com")
elif args[:2] == ["auth", "application-default"] or args[:2] == ["auth", "print-access-token"]:
    print("access-token")
elif args[:2] == ["auth", "print-identity-token"]:
    print("id-token")
elif args[:2] == ["projects", "describe"]:
    print(os.environ.get("STUB_PROJECT_NUMBER", "123456789012"))
elif args[:2] == ["builds", "submit"]:
    print("build log")
elif args[:4] == ["artifacts", "docker", "images", "describe"]:
    print(os.environ.get("STUB_DIGEST") or "sha256:" + hashlib.sha256(args[4].split(":")[0].encode()).hexdigest())
elif args[:3] == ["run", "services", "list"]:
    f = opt("filter")
    if f:
        name = f.split("=", 1)[1]
        for svc in deployed:
            if svc.startswith(name):
                print(f"{svc}\thttps://{svc}-hash1234-uc.a.run.app")
    else:
        for svc in lines("STUB_LIST_RUN"):
            print(svc)
elif args[:3] == ["run", "services", "describe"] and opt("format") == "value(status.url)":
    print(f"https://{args[3]}-hash1234-uc.a.run.app")
elif args[:3] == ["run", "services", "describe"] and opt("format") == "json":
    env = json.loads(os.environ.get("STUB_SERVICE_ENV", "{}"))
    print(json.dumps({"spec": {"template": {"spec": {"containers": [{"env": [{"name": k, "value": v} for k, v in env.items()]}]}}}}))
elif args[:3] == ["secrets", "versions", "access"]:
    print(os.environ.get("STUB_SECRET_VALUE", "placeholder"), end="")
elif args[:3] == ["secrets", "versions", "add"]:
    data = sys.stdin.read()
    with open(os.path.join(d, "secret-data"), "a") as f:
        f.write(data + "\n")
elif args[:2] == ["secrets", "describe"]:
    sys.exit(1 if os.environ.get("STUB_NO_SECRET") else 0)
elif args[:2] == ["secrets", "list"]:
    for n in lines("STUB_LIST_SECRETS"): print(n)
elif args[:3] == ["storage", "buckets", "list"]:
    for n in lines("STUB_LIST_BUCKETS"): print(n)
elif args[:2] == ["storage", "ls"]:
    if "--all-versions" in args:
        b = [a for a in args if a.startswith("gs://")][0]
        if any(b.startswith("gs://" + x + "/") for x in lines("STUB_NONEMPTY_BUCKETS")):
            print(b.replace("**", "obj#1"))
        else:
            sys.stderr.write("ERROR: (gcloud.storage.ls) One or more URLs matched no objects.\n")
            sys.exit(1)
    elif os.environ.get("STUB_OKF"):
        print(f"gs://{os.environ['PROJECT_ID']}-{os.environ['NAME_PREFIX']}-incidents/knowledge/incident-manager/incident-g1.md")
    else:
        sys.stderr.write("ERROR: (gcloud.storage.ls) One or more URLs matched no objects.\n")
        sys.exit(1)
elif args[:2] == ["storage", "cat"]:
    posted = open(os.path.join(d, "posted")).read() if os.path.exists(os.path.join(d, "posted")) else ""
    print(os.environ.get("STUB_OKF", "") + "\n" + posted)
elif args[:3] == ["sql", "instances", "list"]:
    for n in lines("STUB_LIST_SQL"): print(n)
elif args[:3] == ["tasks", "queues", "list"]:
    for n in lines("STUB_LIST_QUEUES"): print(n)
elif args[:3] == ["iam", "service-accounts", "list"]:
    for n in lines("STUB_LIST_SAS"): print(n)
elif args[:3] == ["artifacts", "repositories", "list"]:
    for n in lines("STUB_LIST_REPOS"): print(n)
elif args[:3] == ["artifacts", "packages", "list"]:
    for n in lines("STUB_LIST_PACKAGES"): print(n)
'''

STUB_TERRAFORM = r'''#!/bin/sh
if [ "$1" = "version" ]; then echo "Terraform v1.9.8"; exit 0; fi
python3 -c 'import json,os,sys; open(os.path.join(os.environ["STUB_DIR"],"log"),"a").write(json.dumps(["terraform", os.path.basename(os.getcwd())] + sys.argv[1:]) + "\n")' "$@"
'''

STUB_PSQL = r'''#!/bin/sh
python3 -c 'import json,os,sys; open(os.path.join(os.environ["STUB_DIR"],"log"),"a").write(json.dumps(["psql", "PGPASSWORD=" + ("set" if os.environ.get("PGPASSWORD") else "")] + sys.argv[1:]) + "\n")' "$@"
'''

STUB_PROXY = r'''#!/bin/sh
python3 -c 'import json,os,sys; open(os.path.join(os.environ["STUB_DIR"],"log"),"a").write(json.dumps(["cloud-sql-proxy"] + sys.argv[1:]) + "\n")' "$@"
echo "Listening on 127.0.0.1; The proxy has started successfully and is ready for new connections!"
exec sleep 30
'''

# curl: -o file, -w "%{http_code}", url, -d data. Webhook: wrong token 401; out-of-scope probe
# 200 ignored; an in-scope alert 202. Everything else 200.
STUB_CURL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
d = os.environ["STUB_DIR"]
out = url = data = None
has_w = "-w" in args
configs = []
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-H", "-d", "-X", "--connect-timeout", "--max-time", "--config", "--data-binary"):
        if a == "-o": out = args[i + 1]
        if a == "-d": data = args[i + 1]
        if a == "--data-binary": data = sys.stdin.read() if args[i + 1] == "@-" else args[i + 1]
        if a == "--config":
            p = args[i + 1]
            configs.append({"path": p, "mode": oct(os.stat(p).st_mode & 0o777) if p != "-" else "stdin",
                            "text": sys.stdin.read() if p == "-" else open(p).read()})
        i += 2
        continue
    if not a.startswith("-"):
        url = a
    i += 1
with open(os.path.join(d, "log"), "a") as f:
    f.write(json.dumps(["curl", url, data or ""]) + "\n")
with open(os.path.join(d, "curl-argv"), "a") as f:
    f.write(json.dumps({"argv": args, "configs": configs, "data": data or ""}) + "\n")
code, body = 200, "{}"
if "sqladmin.googleapis.com" in (url or ""):
    body = '{"kind": "sql#operation", "name": "op-1234abcd"}'
if "/webhook/monitoring" in (url or ""):
    if "token=wrong" in url:
        code, body = 401, '{"detail": "Invalid or missing webhook token"}'
    elif "verify-probe" in (data or ""):
        code, body = 200, '{"status": "ignored", "reason": "non-ata alert policy"}'
    else:
        code, body = 202, '{"status": "accepted"}'
        with open(os.path.join(d, "posted"), "a") as f:
            f.write((data or "") + "\n")
if "monitoring.googleapis.com" in (url or ""):
    body = os.environ.get("STUB_MONITORING", "{}")
if out:
    open(out, "w").write(body)
sys.stdout.write(str(code) if has_w else body)
'''

RECORDER = '#!/bin/sh\npython3 -c \'import json,os,sys; open(os.path.join(os.environ["STUB_DIR"],"log"),"a").write(json.dumps(["{name}"] + sys.argv[1:]) + "\\n")\' "$@"\n{extra}\n'


def _copy_tree(tmp_path):
    work = tmp_path / "ata"
    shutil.copytree(ROOT / "scripts", work / "scripts")
    for component in ("agent", "bridge", "tools"):
        (work / component).mkdir()
        shutil.copytree(ROOT / component / "scripts", work / component / "scripts")
        shutil.copy(ROOT / component / "Dockerfile", work / component / "Dockerfile")
    for demo in ("ata-inventory-api", "ata-orders-api"):
        (work / "demo" / demo).mkdir(parents=True)
        shutil.copy(ROOT / "demo" / demo / "Dockerfile", work / "demo" / demo / "Dockerfile")
    (work / "demo" / "db").mkdir()
    shutil.copy(ROOT / "demo" / "db" / "schema.sql", work / "demo" / "db" / "schema.sql")
    shutil.copytree(ROOT / "topology", work / "topology")
    for root in ROOTS:
        (work / "infra" / "terraform" / root).mkdir(parents=True)
    return work


def _recorder(path, name, extra=""):
    path.write_text(RECORDER.format(name=name, extra=extra))
    path.chmod(0o755)


@pytest.fixture
def stack(tmp_path):
    assert TEST_BASH
    work = _copy_tree(tmp_path)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("gcloud", STUB_GCLOUD), ("terraform", STUB_TERRAFORM), ("psql", STUB_PSQL),
                       ("cloud-sql-proxy", STUB_PROXY), ("curl", STUB_CURL)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "bash").symlink_to(TEST_BASH)
    scripts = work / "scripts"
    # Other lanes' scripts (and the verifiers, tested separately) are recorders here.
    _recorder(scripts / "deploy-apigee-proxies.sh", "apigee")
    _recorder(scripts / "teardown-apigee.sh", "teardown-apigee")
    _recorder(scripts / "verify-demo-infra.sh", "verify-demo-infra")
    _recorder(scripts / "repin-inventory.sh", "repin",
              extra='[ "$1" = prepare ] && echo "  $0 write-pin --revision ${NAME_PREFIX}-inventory-api-00001-abc '
                    '--digest $4 --initial"; exit 0')

    def run(script, *args, record_verify_stage1=True, **env_overrides):
        log = stub_dir / "log"
        log.write_text("")
        (stub_dir / "curl-argv").unlink(missing_ok=True)
        if record_verify_stage1 and script != "verify-stage1.sh":
            _recorder(scripts / "verify-stage1.sh", "verify-stage1")
        env = {k: v for k, v in os.environ.items() if not re.match(
            r"^(PROJECT_ID|REGION|NAME_PREFIX|ENVIRONMENT|OWNER|SERVICENOW_.*|APIGEE_.*|GCP_.*|TFSTATE_BUCKET|"
            r"PROJECT_NUMBER|VERIFIER_SA|.*_IMAGE|IMAGE_TAG|ASSUME_YES|PGPASSWORD|BRIDGE_URL|AGENT_URL|TOOLS_URL|"
            r"MODEL_NAME|GEMINI_LOCATION|AUTO_RESOLVE_ON_RECOVERY|DEPLOY_STATE_DIR)$", k)}
        env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", STUB_DIR=str(stub_dir),
                   PROJECT_ID="fresh-proj", REGION="us-central1", NAME_PREFIX="atat", ENVIRONMENT="test",
                   OWNER="team-x", SERVICENOW_INSTANCE_URL="https://dev1.service-now.com", APIGEE_ENV="eval",
                   APIGEE_HOST="api.example.test", SERVICENOW_USERNAME="svc-user", SERVICENOW_PASSWORD="s3cret-PW",
                   IMAGE_TAG="t1", SCHEMA_PROXY_PORT="54399")
        env.update(env_overrides)
        env = {k: v for k, v in env.items() if v is not None}
        res = subprocess.run([TEST_BASH, str(scripts / script), *args], cwd=str(work), capture_output=True,
                             text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return res, calls

    run.work = work
    run.stub_dir = stub_dir
    return run


def _idx(calls, pred, what):
    found = [i for i, c in enumerate(calls) if pred(c)]
    assert found, f"{what} not called: {calls}"
    return found


def _tf(root, action):
    return lambda c: c[:3] == ["terraform", root, action]


def _out(res):
    return res.stdout + res.stderr


# ---------------------------------------------------------------------------
# deploy-stage1.sh
# ---------------------------------------------------------------------------

def test_full_run_order(stack):
    res, calls = stack("deploy-stage1.sh", "--yes")
    assert res.returncode == 0, _out(res)

    first = lambda pred, what: _idx(calls, pred, what)[0]  # noqa: E731
    last = lambda pred, what: _idx(calls, pred, what)[-1]  # noqa: E731
    bootstrap = first(lambda c: c[:3] == ["gcloud", "services", "enable"], "bootstrap")
    foundation = first(_tf("foundation", "apply"), "foundation apply")
    secrets = last(lambda c: c[:4] == ["gcloud", "secrets", "versions", "add"], "secrets add")
    builds = _idx(calls, lambda c: c[:3] == ["gcloud", "builds", "submit"], "builds")
    database = first(_tf("database", "apply"), "database apply")
    psql = first(lambda c: c[0] == "psql", "psql")
    applications = first(_tf("applications", "apply"), "applications apply")
    repin = _idx(calls, lambda c: c[0] == "repin", "repin")
    deploys = [(i, c[3]) for i, c in enumerate(calls) if c[:3] == ["gcloud", "run", "deploy"]]
    triage_iam = first(_tf("triage-iam", "apply"), "triage-iam apply")
    apigee = first(lambda c: c[0] == "apigee", "apigee")
    observability = first(_tf("observability", "apply"), "observability apply")
    verify = first(lambda c: c[0] == "verify-stage1", "verify-stage1")

    assert [name for _, name in deploys] == ["atat-tools", "atat-bridge", "atat-agent"]
    bridge_deploy = deploys[1][0]
    assert bootstrap < foundation < secrets < builds[0]
    assert secrets < bridge_deploy, "ServiceNow secrets must be set before the bridge starts"
    assert len(builds) == 5 and builds[-1] < database < psql < applications < repin[0]
    assert repin[-1] < deploys[0][0]
    assert deploys[-1][0] < triage_iam, "triage-iam needs the triage services"
    assert triage_iam < apigee < observability < verify
    tf_applies = [c[1] for c in calls if c[0] == "terraform" and c[2] == "apply"]
    assert tf_applies[-1] == "observability"
    assert [c[1] for c in calls if c[0] == "repin"] == ["prepare", "write-pin", "switch"]
    assert calls[repin[0]][1:] == ["prepare", "--initial", "--image-digest",
                                   calls[repin[1]][calls[repin[1]].index("--digest") + 1]]
    assert calls[repin[1]][1:4] == ["write-pin", "--revision", "atat-inventory-api-00001-abc"]


def test_terraform_roots_reconfigure_and_get_the_vars(stack):
    res, calls = stack("deploy-stage1.sh", "--yes")
    assert res.returncode == 0, _out(res)
    for root in ROOTS:
        init = [c for c in calls if c[:3] == ["terraform", root, "init"]]
        apply = [c for c in calls if c[:3] == ["terraform", root, "apply"]]
        assert init and "-reconfigure" in init[0] and "-backend-config=backend.hcl" in init[0], root
        assert apply, root
        for v in ("project_id=fresh-proj", "region=us-central1", "name_prefix=atat"):
            assert v in apply[0], (root, apply[0])
    app = [c for c in calls if c[:3] == ["terraform", "applications", "apply"]][0]
    inv = [a for a in app if a.startswith("inventory_image=")][0]
    assert re.fullmatch(r"inventory_image=us-central1-docker\.pkg\.dev/fresh-proj/atat-docker/atat-inventory-api@sha256:[0-9a-f]{64}", inv)
    assert any(a.startswith("orders_image=us-central1-docker.pkg.dev/fresh-proj/atat-docker/atat-orders-api@sha256:") for a in app)
    for root in ("foundation", "database", "applications"):
        a = [c for c in calls if c[:3] == ["terraform", root, "apply"]][0]
        assert "environment=test" in a and "owner=team-x" in a
    obs = [c for c in calls if c[:3] == ["terraform", "observability", "apply"]][0]
    assert not [a for a in obs if a.startswith(("environment=", "owner="))]


def test_triage_deploys_use_the_built_images(stack):
    res, calls = stack("deploy-stage1.sh", "--yes")
    assert res.returncode == 0, _out(res)
    builds = [c for c in calls if c[:3] == ["gcloud", "builds", "submit"]]
    assert len(builds) == 5  # the deploy scripts did not build again
    for c in [c for c in calls if c[:3] == ["gcloud", "run", "deploy"]]:
        image = [a for a in c if a.startswith("--image=")][0]
        assert re.fullmatch(rf"--image=us-central1-docker\.pkg\.dev/fresh-proj/atat-docker/{c[3]}@sha256:[0-9a-f]{{64}}", image)


def _curl_calls(stack):
    path = stack.stub_dir / "curl-argv"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_schema_uses_the_proxy_and_grants_the_iam_user(stack):
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "schema")
    assert res.returncode == 0, _out(res)
    setpw = [c for c in _curl_calls(stack) if "sqladmin.googleapis.com" in json.dumps(c["argv"])]
    assert len(setpw) == 1
    assert setpw[0]["argv"][-1] == ("https://sqladmin.googleapis.com/v1/projects/fresh-proj/instances/"
                                    "atat-inventory-db/users?name=postgres")
    proxy = [c for c in calls if c[0] == "cloud-sql-proxy"][0]
    assert proxy[1:] == ["--port", "54399", "fresh-proj:us-central1:atat-inventory-db"]
    psql = [c for c in calls if c[0] == "psql"][0]
    assert psql[1] == "PGPASSWORD=set"
    assert "iam_user=atat-inventory-sa@fresh-proj.iam" in psql and "ON_ERROR_STOP=1" in psql
    assert psql[-1].endswith("demo/db/schema.sql")
    # The admin call finishes before the proxy starts.
    wait = _idx(calls, lambda c: c[:4] == ["gcloud", "sql", "operations", "wait"], "operations wait")[0]
    assert calls[wait][4] == "op-1234abcd"
    assert wait < _idx(calls, lambda c: c[0] == "cloud-sql-proxy", "proxy")[0]


def test_schema_password_and_token_stay_off_argv_and_out_of_the_output(stack, tmp_path):
    """sec M1: no `--password=` on any argv; the password only in curl's stdin body; the bearer
    token only in a 0600 curl config file that is gone after the run."""
    (tmp_path / "t").mkdir()
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "schema", TMPDIR=str(tmp_path / "t"))
    assert res.returncode == 0, _out(res)
    flat = json.dumps(calls)
    assert "--password" not in flat
    assert not [c for c in calls if c[:4] == ["gcloud", "sql", "users", "set-password"]]
    setpw = [c for c in _curl_calls(stack) if "sqladmin.googleapis.com" in json.dumps(c["argv"])][0]
    body = json.loads(setpw["data"])
    password = body["password"]
    assert body["name"] == "postgres" and len(password) == 32
    for c in calls:
        assert not any(password in a for a in c[1:] if c[0] != "curl"), c
    assert password not in json.dumps(setpw["argv"]) and password not in _out(res)
    # The stub ran from the log: its "data" field is the stdin body, never an argv value.
    assert "@-" in setpw["argv"]
    assert "access-token" not in json.dumps(setpw["argv"])
    assert [c["mode"] for c in setpw["configs"]] == ["0o600"]
    assert setpw["configs"][0]["text"] == 'header = "Authorization: Bearer access-token"\n'
    assert not Path(setpw["configs"][0]["path"]).exists(), "curl config left behind"
    assert not list((tmp_path / "t").glob("atat-sqladmin.*")), "temp files left behind"


def test_schema_fails_loudly_when_the_admin_call_fails(stack, tmp_path):
    curl = tmp_path / "bin" / "curl"
    curl.write_text("#!/bin/sh\ncat >/dev/null\necho 'curl: (22) The requested URL returned error: 403' >&2\nexit 22\n")
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "schema")
    assert res.returncode == 1
    assert "users.update (postgres on atat-inventory-db) failed" in res.stderr
    assert not [c for c in calls if c[0] in ("cloud-sql-proxy", "psql")]


def test_schema_with_pgpassword_does_not_reset_the_password(stack):
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "schema", PGPASSWORD="given")
    assert res.returncode == 0, _out(res)
    assert not [c for c in calls if c[:4] == ["gcloud", "sql", "users", "set-password"]]
    assert not [c for c in _curl_calls(stack) if "sqladmin.googleapis.com" in json.dumps(c["argv"])]


def test_schema_fails_loudly_without_psql(stack, tmp_path):
    (tmp_path / "bin" / "psql").unlink()
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "schema",
                       PATH=f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")
    assert res.returncode == 1
    assert "psql not on PATH" in res.stderr


@pytest.mark.parametrize("missing", ["PROJECT_ID", "REGION", "NAME_PREFIX", "ENVIRONMENT", "OWNER",
                                     "SERVICENOW_INSTANCE_URL", "APIGEE_ENV", "APIGEE_HOST"])
def test_required_inputs(stack, missing):
    res, calls = stack("deploy-stage1.sh", "--yes", **{missing: None})
    assert res.returncode == 1
    assert missing in res.stderr and "no defaults" in res.stderr
    assert calls == []


def test_apigee_org_defaults_to_project_and_is_echoed_and_exported(stack, tmp_path):
    _recorder(stack.work / "scripts" / "deploy-apigee-proxies.sh", "apigee", extra='echo "ORG=$APIGEE_ORG ENV=$APIGEE_ENV HOST=$APIGEE_HOST"')
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "apigee")
    assert res.returncode == 0, _out(res)
    assert "APIGEE_ORG=fresh-proj (defaulted to PROJECT_ID)" in res.stdout
    assert "ORG=fresh-proj ENV=eval HOST=api.example.test" in res.stdout


def test_invalid_prefix_and_phase_arguments(stack):
    res, calls = stack("deploy-stage1.sh", "--yes", NAME_PREFIX="Bad_X")
    assert res.returncode == 1 and "NAME_PREFIX 'Bad_X' is invalid" in res.stderr and calls == []
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "nope")
    assert res.returncode == 1 and "unknown phase 'nope'" in res.stderr and calls == []
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "images", "--from", "database")
    assert res.returncode == 1 and "--only cannot be combined" in res.stderr
    res, calls = stack("deploy-stage1.sh", "--yes", "--from", "verify", "--to", "bootstrap")
    assert res.returncode == 1 and "comes after" in res.stderr


def test_list_phases_is_the_stable_order(stack):
    res, _ = stack("deploy-stage1.sh", "--list-phases")
    assert res.stdout.split() == ["preflight", "bootstrap", "foundation", "secrets", "images", "database", "schema",
                                  "applications", "inventory-pin", "triage", "triage-iam", "apigee", "observability",
                                  "verify"]


def test_without_yes_and_without_a_terminal_nothing_runs(stack):
    res, calls = stack("deploy-stage1.sh")
    assert res.returncode == 1
    assert "re-run with --yes" in res.stderr
    assert calls == []


def test_images_file_carries_refs_to_a_resumed_run(stack, tmp_path):
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "images")
    assert res.returncode == 0, _out(res)
    images = (stack.work / ".deploy-state" / "fresh-proj-atat-images.env").read_text()
    for var in ("INVENTORY_IMAGE", "ORDERS_IMAGE", "AGENT_IMAGE", "BRIDGE_IMAGE", "TOOLS_IMAGE"):
        assert re.search(rf"^{var}=us-central1-docker\.pkg\.dev/fresh-proj/atat-docker/atat-[a-z-]+@sha256:[0-9a-f]{{64}}$", images, re.M)
    assert (stack.work / ".deploy-state" / ".gitignore").read_text() == "*\n"
    # bootstrap writes backend.hcl; a later run resumes at applications from the file.
    assert stack("bootstrap-tfstate.sh", "--write-backend-only")[0].returncode == 0
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "applications")
    assert res.returncode == 0, _out(res)
    app = [c for c in calls if c[:3] == ["terraform", "applications", "apply"]][0]
    assert any(a.startswith("inventory_image=us-central1-docker.pkg.dev/fresh-proj/atat-docker/atat-inventory-api@sha256:") for a in app)
    assert not [c for c in calls if c[:3] == ["gcloud", "builds", "submit"]]


def test_resume_without_image_refs_fails_with_the_fix(stack):
    assert stack("bootstrap-tfstate.sh", "--write-backend-only")[0].returncode == 0
    res, calls = stack("deploy-stage1.sh", "--yes", "--from", "applications")
    assert res.returncode == 1
    assert "image ref(s) unknown: INVENTORY_IMAGE ORDERS_IMAGE" in res.stderr
    assert "--only images" in res.stderr
    assert not [c for c in calls if c[0] == "terraform" and "apply" in c]


def test_refuses_another_stacks_backend(stack):
    assert stack("bootstrap-tfstate.sh", "--write-backend-only", NAME_PREFIX="ata")[0].returncode == 0
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "database")
    assert res.returncode == 1
    assert "is not for NAME_PREFIX=atat" in res.stderr
    assert not [c for c in calls if c[0] == "terraform"]


def test_inventory_pin_needs_the_repin_script(stack):
    (stack.work / "scripts" / "repin-inventory.sh").unlink()
    res, calls = stack("deploy-stage1.sh", "--yes", "--only", "inventory-pin",
                       INVENTORY_IMAGE="us-central1-docker.pkg.dev/fresh-proj/atat-docker/atat-inventory-api@sha256:" + "0" * 64)
    assert res.returncode == 1
    assert "repin-inventory.sh" in res.stderr and "prepare --initial" in res.stderr


def test_each_phase_prints_its_check_hints(stack):
    res, _ = stack("deploy-stage1.sh", "--yes", "--only", "database")
    assert "--- Check (database) ---" in res.stdout or res.returncode != 0
    assert stack("bootstrap-tfstate.sh", "--write-backend-only")[0].returncode == 0
    res, _ = stack("deploy-stage1.sh", "--yes", "--only", "database")
    assert res.returncode == 0, _out(res)
    assert "--- Check (database) ---" in res.stdout
    assert "gcloud sql instances describe atat-inventory-db --project=fresh-proj" in res.stdout


def test_no_container_specific_path_and_no_live_identifiers():
    for rel in ("scripts/deploy-stage1.sh", "scripts/teardown-stack.sh", "scripts/build-image.sh",
                "scripts/set-secrets.sh", "scripts/verify-stage1.sh", "scripts/verify-demo-infra.sh",
                "scripts/cloud-run-deploy-lib.sh", "agent/scripts/deploy.sh", "bridge/scripts/deploy.sh",
                "tools/scripts/deploy.sh"):
        text = (ROOT / rel).read_text()
        assert "/home/scion" not in text, rel
        assert not re.search(r"cloud-assist-fde-2|185099261133|4klvlqnmpa|nip\.io", text), rel
        assert not re.search(r'PROJECT_ID="\$\{PROJECT_ID:-[^}$]', text), rel


def test_deploy_demo_infra_forwards_to_deploy_stage1(stack):
    res, calls = stack("deploy-demo-infra.sh", "--yes", NAME_PREFIX=None)
    assert res.returncode == 1
    assert "running scripts/deploy-stage1.sh --from database --yes" in res.stdout
    assert "NAME_PREFIX" in res.stderr


# ---------------------------------------------------------------------------
# build-image.sh
# ---------------------------------------------------------------------------

def test_build_image_prints_the_digest_line_and_uses_the_build_sa(stack):
    res, calls = stack("build-image.sh", "--yes", str(stack.work / "demo" / "ata-orders-api"), "atat-orders-api")
    assert res.returncode == 0, _out(res)
    lines = res.stdout.strip().splitlines()
    assert len(lines) == 1 and re.fullmatch(
        r"IMAGE=us-central1-docker\.pkg\.dev/fresh-proj/atat-docker/atat-orders-api@sha256:[0-9a-f]{64}", lines[0])
    b = [c for c in calls if c[:3] == ["gcloud", "builds", "submit"]][0]
    assert "--service-account=projects/fresh-proj/serviceAccounts/atat-build-sa@fresh-proj.iam.gserviceaccount.com" in b
    assert "--gcs-source-staging-dir=gs://fresh-proj-atat-build/source" in b
    assert "--gcs-log-dir=gs://fresh-proj-atat-build/logs" in b
    assert "--tag=us-central1-docker.pkg.dev/fresh-proj/atat-docker/atat-orders-api:t1" in b
    d = [c for c in calls if c[:4] == ["gcloud", "artifacts", "docker", "images"]][0]
    assert "--format=value(image_summary.digest)" in d


def test_build_image_rejects_a_bad_digest(stack):
    res, _ = stack("build-image.sh", "--yes", str(stack.work / "demo" / "ata-orders-api"), "atat-orders-api",
                   STUB_DIGEST="sha256:xyz")
    assert res.returncode == 1
    assert "IMAGE=" not in res.stdout
    assert "unexpected digest" in res.stderr


def test_build_image_syncs_topology_only_for_the_build(stack):
    res, _ = stack("build-image.sh", "--yes", str(stack.work / "agent"), "atat-agent")
    assert res.returncode == 0, _out(res)
    assert "Syncing the topology package" in res.stderr
    assert not (stack.work / "agent" / "topology").exists()


def test_build_image_requires_inputs(stack):
    res, calls = stack("build-image.sh", "--yes", "x", "y", PROJECT_ID=None)
    assert res.returncode == 1 and "PROJECT_ID" in res.stderr and calls == []


# ---------------------------------------------------------------------------
# set-secrets.sh
# ---------------------------------------------------------------------------

def test_set_secrets_requires_project_and_prefix(stack):
    for var in ("PROJECT_ID", "NAME_PREFIX"):
        res, calls = stack("set-secrets.sh", "--yes", **{var: None})
        assert res.returncode == 1 and var in res.stderr and calls == []


def test_set_secrets_never_prints_values_and_writes_both(stack):
    res, calls = stack("set-secrets.sh", "--yes")
    assert res.returncode == 0, _out(res)
    assert "s3cret-PW" not in _out(res) and "svc-user" not in _out(res)
    adds = [c for c in calls if c[:4] == ["gcloud", "secrets", "versions", "add"]]
    assert [c[4] for c in adds] == ["atat-servicenow-username", "atat-servicenow-password"]
    assert (stack.stub_dir / "secret-data").read_text() == "svc-user\ns3cret-PW\n"


def test_set_secrets_skip_if_set_leaves_real_values(stack):
    res, calls = stack("set-secrets.sh", "--yes", "--skip-if-set", STUB_SECRET_VALUE="real-value")
    assert res.returncode == 0, _out(res)
    assert "real-value" not in _out(res)
    assert not [c for c in calls if c[:4] == ["gcloud", "secrets", "versions", "add"]]


def test_set_secrets_missing_secret_fails_unless_create_missing(stack):
    res, calls = stack("set-secrets.sh", "--yes", STUB_NO_SECRET="1")
    assert res.returncode == 1 and "apply foundation first" in res.stderr
    assert not [c for c in calls if c[:3] == ["gcloud", "secrets", "create"]]
    res, calls = stack("set-secrets.sh", "--yes", "--create-missing", STUB_NO_SECRET="1")
    assert res.returncode == 0, _out(res)
    create = [c for c in calls if c[:3] == ["gcloud", "secrets", "create"]][0]
    labels = dict(kv.split("=", 1) for kv in [a for a in create if a.startswith("--labels=")][0][9:].split(","))
    assert labels["app"] == "alert-triage-agent" and labels["stack"] == "atat" and labels["env"] == "test"


def test_set_secrets_without_values_or_terminal_fails(stack):
    res, calls = stack("set-secrets.sh", "--yes", SERVICENOW_USERNAME=None)
    assert res.returncode == 1 and "SERVICENOW_USERNAME is not set" in res.stderr
    assert not [c for c in calls if c[:4] == ["gcloud", "secrets", "versions", "add"]]


# ---------------------------------------------------------------------------
# verify-stage1.sh
# ---------------------------------------------------------------------------

AGENT_ENV = {"BRIDGE_URL": "https://atat-bridge-hash1234-uc.a.run.app", "TOOLS_URL": "https://atat-tools-hash1234-uc.a.run.app",
             "SERVICE_ACCOUNT_EMAIL": "atat-agent-sa@fresh-proj.iam.gserviceaccount.com"}
DEPLOYED = "atat-inventory-api atat-orders-api atat-agent atat-bridge atat-tools"
OKF = "---\ntype: incident\ngroup_id: g1\nstatus: ticketed\nalerts:\n- incident_id: x\n---\n"


def _verify(stack, *args, **env):
    env.setdefault("STUB_DEPLOYED", DEPLOYED)
    env.setdefault("STUB_SERVICE_ENV", json.dumps(AGENT_ENV))
    env.setdefault("STUB_SECRET_VALUE", "hook-token")
    return stack("verify-stage1.sh", *args, record_verify_stage1=False, **env)


def test_verify_default_creates_nothing(stack):
    res, calls = _verify(stack)
    assert res.returncode == 0, _out(res)
    posts = [c for c in calls if c[0] == "curl" and c[2]]
    assert not [c for c in calls if c[0] == "curl" and "/triage" in c[1]]
    assert len(posts) == 2
    assert all("verify-probe" in c[2] and "atat-" not in c[2] and "ata-" not in c[2] for c in posts)
    assert "200 ignored" in res.stdout and "wrong token: 401" in res.stdout
    assert "no incident documents yet" in res.stdout


def test_verify_checks_the_okf_schema(stack):
    res, _ = _verify(stack, STUB_OKF=OKF)
    assert res.returncode == 0, _out(res)
    assert "type=incident group_id=g1 status=ticketed" in res.stdout
    res, _ = _verify(stack, STUB_OKF="---\ntype: incident\nstatus: mitigation-in-progress\n---\n")
    assert res.returncode == 1
    assert "unknown status 'mitigation-in-progress'" in res.stderr


def test_verify_with_ticket_accepts_202_and_cleans_up(stack):
    res, calls = _verify(stack, "--with-ticket", "--wait-secs", "0", STUB_OKF=OKF)
    assert res.returncode == 0, _out(res)
    posted = [json.loads(c[2]) for c in calls if c[0] == "curl" and c[2] and "inventory-5xx" in c[2]]
    assert [p["incident"]["state"] for p in posted] == ["open", "closed"]
    assert posted[0]["incident"]["policy_name"] == "atat-inventory-5xx"
    assert "alert 0.verify_e2e_" in res.stdout and "HTTP 202" in res.stdout
    assert "close it in ServiceNow" in res.stdout


def test_verify_fails_on_a_missing_service_and_a_foreign_agent_config(stack):
    res, _ = _verify(stack, STUB_DEPLOYED="atat-agent atat-bridge")
    assert res.returncode == 1 and "atat-inventory-api is not deployed" in res.stderr
    res, _ = _verify(stack, STUB_SERVICE_ENV=json.dumps(dict(AGENT_ENV, BRIDGE_URL="https://other-bridge.example")))
    assert res.returncode == 1 and "agent BRIDGE_URL='https://other-bridge.example'" in res.stderr


def test_verify_requires_inputs(stack):
    res, calls = _verify(stack, PROJECT_ID=None)
    assert res.returncode == 1 and "PROJECT_ID" in res.stderr and calls == []


# ---------------------------------------------------------------------------
# teardown-stack.sh
# ---------------------------------------------------------------------------

MUTATING = ("delete", "rm", "destroy", "apply", "set-password", "add", "create", "update", "deploy")


def _mutations(calls):
    out = []
    for c in calls:
        if c[0] == "terraform" and c[2] in ("destroy", "apply"):
            out.append(c)
        elif c[0] == "gcloud" and any(m in c[1:4] for m in MUTATING):
            out.append(c)
        elif c[0] == "teardown-apigee" and "--apply" in c:
            out.append(c)
    return out


TEARDOWN_ENV = dict(
    STUB_LIST_RUN="ata-agent,atat-agent,atat-bridge,atat-tools,other-svc",
    STUB_LIST_BUCKETS="fresh-proj-ata-incidents,fresh-proj-atat-incidents,fresh-proj-atat-build,fresh-proj-atat-tfstate",
    STUB_LIST_REPOS="projects/fresh-proj/locations/us-central1/repositories/atat-docker",
    STUB_LIST_PACKAGES="atat-agent,atat-bridge",
    STUB_NONEMPTY_BUCKETS="fresh-proj-atat-incidents,fresh-proj-atat-build,fresh-proj-ata-incidents",
)


def test_teardown_dry_run_changes_nothing(stack):
    res, calls = stack("teardown-stack.sh", **TEARDOWN_ENV)
    assert res.returncode == 0, _out(res)
    assert _mutations(calls) == []
    assert ["teardown-apigee"] in calls
    assert "[dry-run] gcloud run services delete atat-agent" in res.stdout
    assert "Dry run only" in res.stdout


def test_teardown_apply_order_and_prefix_guard(stack):
    assert stack("bootstrap-tfstate.sh", "--write-backend-only")[0].returncode == 0
    after_env = dict(TEARDOWN_ENV)
    res, calls = stack("teardown-stack.sh", "--apply", "--confirm-prefix", "atat", **after_env)
    muts = _mutations(calls)
    # Every mutating call names only atat resources: never an "ata-" one (or anything else).
    for c in muts:
        for a in c:
            assert not re.search(r"(?<![a-z0-9])ata-", a), c
            assert "other-svc" not in a
    order = []
    for c in muts:
        if c[0] == "teardown-apigee":
            order.append("apigee")
        elif c[0] == "terraform":
            order.append(f"tf:{c[1]}")
        elif c[1:4] == ["run", "services", "delete"]:
            order.append(f"run:{c[4]}")
        elif c[1:3] == ["storage", "rm"]:
            order.append("empty:" + [a for a in c if a.startswith("gs://")][0])
        elif c[1:4] == ["artifacts", "packages", "delete"]:
            order.append(f"ar:{c[4]}")
    assert order == ["apigee", "tf:observability", "tf:triage-iam", "run:atat-agent", "run:atat-bridge", "run:atat-tools",
                     "tf:applications", "tf:database", "empty:gs://fresh-proj-atat-incidents/**",
                     "empty:gs://fresh-proj-atat-build/**", "ar:atat-agent", "ar:atat-bridge", "tf:foundation"], order
    assert [c for c in calls if c[0] == "teardown-apigee"][0][1:] == ["--apply", "--yes"]
    # The nothing-left check: the listed atat-* services remain in the stub, so it fails.
    assert res.returncode == 1
    assert "REMAINING run-service atat-agent" in res.stdout
    assert "ata-agent" not in [line.split()[-1] for line in res.stdout.splitlines() if "REMAINING" in line]
    assert "kept      bucket fresh-proj-atat-tfstate" in res.stdout
    assert not [c for c in calls if any("tfstate" in a for a in c) and c[1:3] == ["storage", "rm"]]


def test_teardown_nothing_left_passes_and_delete_state_is_last(stack):
    assert stack("bootstrap-tfstate.sh", "--write-backend-only")[0].returncode == 0
    res, calls = stack("teardown-stack.sh", "--apply", "--confirm-prefix", "atat", "--delete-state",
                       STUB_LIST_RUN="ata-agent,other-svc", STUB_LIST_BUCKETS="fresh-proj-ata-incidents",
                       STUB_LIST_SECRETS="projects/1/secrets/ata-webhook-token", STUB_LIST_SAS="ata-agent-sa@fresh-proj.iam.gserviceaccount.com")
    assert res.returncode == 0, _out(res)
    assert "nothing named atat-* is left" in res.stdout
    muts = _mutations(calls)
    assert muts[-1][1:3] == ["storage", "rm"] and "gs://fresh-proj-atat-tfstate" in muts[-1] and "--all-versions" in muts[-1]
    tf_foundation = [i for i, c in enumerate(muts) if c[:3] == ["terraform", "foundation", "destroy"]][0]
    assert tf_foundation == len(muts) - 2


@pytest.mark.parametrize("prefix", ["", "A", "ata-x", "toolongprefix1"])
def test_teardown_guards_the_prefix(stack, prefix):
    res, calls = stack("teardown-stack.sh", "--apply", "--confirm-prefix", prefix, NAME_PREFIX=prefix)
    assert res.returncode == 1
    assert calls == []


def test_teardown_confirmation_must_match(stack):
    res, calls = stack("teardown-stack.sh", "--apply", "--confirm-prefix", "ata")
    assert res.returncode == 1 and "does not match" in res.stderr
    assert _mutations(calls) == []
    res, calls = stack("teardown-stack.sh", "--apply")
    assert res.returncode == 1 and "--confirm-prefix atat" in res.stderr
    assert calls == []


# ---------------------------------------------------------------------------
# verify-demo-infra.sh (static), bridge VERIFIER_SA validation, run-all preflight
# ---------------------------------------------------------------------------

def test_verify_demo_infra_has_no_fallback_urls_and_prefix_base_paths():
    text = (ROOT / "scripts" / "verify-demo-infra.sh").read_text()
    assert "4klvlqnmpa" not in text and "/ata/" not in text
    assert 'INV_BASE="/${NAME_PREFIX}/inventory/v1"' in text
    assert 'ORD_BASE="/${NAME_PREFIX}/orders/v1"' in text


def test_run_all_skips_preflight_after_an_interrupt():
    text = (ROOT / "scripts" / "run-all-scenarios.sh").read_text()
    assert 'if [ "${SKIP_PREFLIGHT:-false}" != "true" ] && [ "${INTERRUPTED}" != "true" ]; then' in text


def test_bridge_verifier_sa_is_validated_and_lowercased(monkeypatch):
    from bridge.app.config import BridgeConfig

    cfg = BridgeConfig(VERIFIER_SA=" Ops@Proj-X.iam.gserviceaccount.com , qa@example.com ")
    assert cfg.verifier_sa == "ops@proj-x.iam.gserviceaccount.com,qa@example.com"
    assert BridgeConfig(VERIFIER_SA="").verifier_sa is None
    for bad in ("none", "ops", "ops@", "a@b@c.com", "ops@example.com,not an email"):
        with pytest.raises(ValueError, match="VERIFIER_SA entries must be email addresses"):
            BridgeConfig(VERIFIER_SA=bad)
