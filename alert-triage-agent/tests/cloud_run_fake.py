"""Stateful fake `gcloud` (Cloud Run subset) and `curl` for the re-pin, instance-cap and
revision-prune script tests. State lives in <bin>/state.json; every call is logged as one
JSON argv line in <bin>/calls.log (gcloud) or <bin>/curl.log (curl).

Modelled Cloud Run behaviour (as documented by `gcloud run ... --help`):
- `services update` creates a revision from the template plus the flags. A traffic split
  pinned by name keeps the new revision at 0%; a `latestRevision` entry follows it.
  `--no-traffic` never routes to it; `--tag` adds a tag entry with a URL. `--max` alone
  (service-level limit) creates no revision.
- `services update-traffic --to-revisions=R=100` replaces the percent entries and keeps
  tags; `--remove-tags` drops tag entries.
- `revisions delete` refuses a revision that serves traffic.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))

PROJECT = "cloud-assist-fde-2"
INV = "ata-inventory-api"
ORD = "ata-orders-api"
OLD_PIN = "ata-inventory-api-00073-9vb"
OLD_DIGEST = "sha256:24b07eaa50db6d226889607116f0dd43693df5e1f597867a79e0f86e3d7767ce"
NEW_DIGEST = "sha256:" + "ab" * 32
IMAGE_BASE = f"us-west1-docker.pkg.dev/{PROJECT}/ata-docker/ata-inventory-api"
INV_URL = "https://inv.example"
ORD_URL = "https://ord.example"

BASELINE_ENV = {
    "ATA_INVENTORY_INSTANCE_CONNECTION_NAME": f"{PROJECT}:us-west1:ata-inventory-db",
    "ATA_INVENTORY_DB_USER": f"ata-inventory-sa@{PROJECT}.iam",
    "ATA_INVENTORY_DB_NAME": "inventory",
    "ATA_INVENTORY_DB_IAM_AUTH": "true",
    "ATA_INVENTORY_MOCK_DB": "false",
}
LEGACY_ENV = dict(BASELINE_ENV, DB_USER=f"ata-inventory-sa@{PROJECT}.iam", MOCK_DB="false")

FAKE_GCLOUD = r'''
import json, sys
from pathlib import Path

here = Path(__file__).parent
state_path = here / "state.json"
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with open(here / "calls.log", "a") as log:
    log.write(json.dumps(args) + "\n")

def opt(name):
    for a in args:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None

def has(flag):
    return f"--{flag}" in args

def save():
    state_path.write_text(json.dumps(state))

def fail_if(verb):
    if verb in state["fail"]:
        print(f"ERROR: (gcloud) fake failure: {verb}", file=sys.stderr)
        sys.exit(1)

def annotations(max_scale, min_scale):
    a = {"autoscaling.knative.dev/maxScale": max_scale}
    if min_scale is not None:
        a["autoscaling.knative.dev/minScale"] = min_scale
    return a

def revision_doc(name):
    r = state["revisions"][name]
    return {
        "metadata": {"name": name, "creationTimestamp": r["created"],
                     "labels": {"serving.knative.dev/service": r["service"]},
                     "annotations": annotations(r["max"], r["min"])},
        "spec": {"containers": [{"image": r["image"],
                                 "env": [{"name": k, "value": v} for k, v in r["env"].items()]}]},
        "status": {"imageDigest": r["image"].split("@")[0] + "@" + r["digest"],
                   "conditions": [{"type": "Ready", "status": "True" if r["ready"] else "False"}]},
    }

def service_doc(name):
    s = state["services"][name]
    t = s["template"]
    return {
        "metadata": {"name": name, "annotations": {"run.googleapis.com/maxScale": s["service_max"]}},
        "spec": {"template": {"metadata": {"annotations": annotations(t["max"], t["min"])},
                              "spec": {"containers": [{"image": t["image"],
                                                       "env": [{"name": k, "value": v} for k, v in t["env"].items()]}]}}},
        "status": {"url": s["url"], "traffic": s["traffic"],
                   "latestCreatedRevisionName": s["latest"], "latestReadyRevisionName": s["latest"]},
    }

fmt = opt("format") or ""
if args[:2] == ["auth", "print-identity-token"]:
    print("fake-token")
elif args[:2] == ["builds", "submit"]:
    fail_if("build")
    state["built_tag"] = opt("tag")
    save()
    print(state["build_digest"])
elif args[:3] == ["run", "services", "describe"]:
    fail_if("describe")
    name = args[3]
    doc = service_doc(name)
    if fmt == "json":
        print(json.dumps(doc))
    elif fmt == "value(status.url)":
        print(doc["status"]["url"])
    elif fmt.startswith("csv[no-heading]"):
        pct = [t for t in doc["status"]["traffic"]]
        print(";".join(t.get("revisionName", "") for t in pct) + "," + ";".join(str(t.get("percent", "")) for t in pct))
    else:
        sys.exit(f"fake gcloud: unhandled describe format {fmt}")
elif args[:3] == ["run", "services", "update"]:
    fail_if("update")
    name = args[3]
    s = state["services"][name]
    if opt("max") is not None:
        s["service_max"] = opt("max")
    revision_flags = [a for a in args[4:] if not a.startswith(("--project=", "--region=", "--quiet", "--max="))]
    if revision_flags:
        t = s["template"]
        if opt("image"):
            t["image"] = opt("image")
        for k in (opt("remove-env-vars") or "").split(","):
            t["env"].pop(k, None)
        if opt("update-env-vars"):
            t["env"].update(dict(kv.split("=", 1) for kv in opt("update-env-vars").split(",")))
        if opt("max-instances") is not None and "ignore-max-instances" not in state["fail"]:
            t["max"] = opt("max-instances")
        if opt("min-instances") is not None:
            t["min"] = opt("min-instances")
        state["counter"] += 1
        rev = f"{name}-{opt('revision-suffix') or format(state['counter'], '05d') + '-new'}"
        state["revisions"][rev] = {"service": name, "image": t["image"], "digest": t["image"].split("@")[-1],
                                   "env": dict(t["env"]), "max": t["max"], "min": t["min"],
                                   "ready": "new-not-ready" not in state["fail"],
                                   "created": f"2026-10-28T00:00:{state['counter'] % 60:02d}Z"}
        s["latest"] = rev
        if not has("no-traffic"):
            for e in s["traffic"]:
                if e.get("latestRevision"):
                    e["revisionName"] = rev
        if opt("tag"):
            s["traffic"].append({"revisionName": rev, "tag": opt("tag"),
                                 "url": "https://" + opt("tag") + "---" + s["url"][len("https://"):]})
    save()
elif args[:3] == ["run", "services", "update-traffic"]:
    fail_if("update-traffic")
    s = state["services"][args[3]]
    if opt("to-revisions") and "ignore-traffic" not in state["fail"]:
        rev, pct = opt("to-revisions").split("=")
        s["traffic"] = [{"revisionName": rev, "percent": int(pct)}] + [e for e in s["traffic"] if e.get("tag")]
    if opt("remove-tags"):
        fail_if("remove-tags")
        drop = set(opt("remove-tags").split(","))
        s["traffic"] = [e for e in s["traffic"] if e.get("tag") not in drop]
    save()
elif args[:3] == ["run", "revisions", "describe"]:
    rev = args[3]
    if "revisions-describe" in state["fail"]:
        print("ERROR: (gcloud.run.revisions.describe) PERMISSION_DENIED: permission denied", file=sys.stderr)
        sys.exit(1)
    if rev not in state["revisions"]:
        print(f"ERROR: revision {rev} not found", file=sys.stderr)
        sys.exit(1)
    if fmt == "json":
        print(json.dumps(revision_doc(rev)))
    elif fmt == "value(status.imageDigest)":
        print(revision_doc(rev)["status"]["imageDigest"])
    elif fmt == "value(metadata.name)":
        print(rev)
    else:
        sys.exit(f"fake gcloud: unhandled revisions describe format {fmt}")
elif args[:3] == ["run", "revisions", "list"]:
    fail_if("list")
    svc = opt("service")
    docs = [revision_doc(n) for n, r in state["revisions"].items() if r["service"] == svc]
    print(json.dumps(docs))
elif args[:3] == ["run", "revisions", "delete"]:
    rev = args[3]
    if "delete:" + rev in state["fail"]:
        print(f"ERROR: fake delete failure {rev}", file=sys.stderr)
        sys.exit(1)
    svc = state["revisions"][rev]["service"]
    if any(e.get("revisionName") == rev and (e.get("percent") or 0) > 0 for e in state["services"][svc]["traffic"]):
        print(f"ERROR: revision {rev} is receiving traffic", file=sys.stderr)
        sys.exit(1)
    del state["revisions"][rev]
    save()
else:
    sys.exit(f"fake gcloud: unhandled {args}")
'''

# curl: answers from state.json["http"]: {"<base url>": {"<METHOD> <path>": [code, body]}}.
# A base URL without an entry for the path answers 404.
FAKE_CURL = r'''
import json, sys
from pathlib import Path

here = Path(__file__).parent
state = json.loads((here / "state.json").read_text())
args = sys.argv[1:]
with open(here / "curl.log", "a") as log:
    log.write(json.dumps(args) + "\n")
out, fmt, method, url, data = None, "", "GET", None, None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-X", "-d", "-H", "--connect-timeout", "--max-time", "--data"):
        val = args[i + 1]
        if a == "-o": out = val
        elif a == "-w": fmt = val
        elif a == "-X": method = val
        elif a in ("-d", "--data"): data = val
        i += 2
        continue
    if a.startswith("http"):
        url = a
    i += 1
if data is not None and method == "GET":
    method = "POST"
base = url.split("/", 3)
base_url, path = "/".join(base[:3]), "/" + (base[3] if len(base) > 3 else "")
# state["http_rev"] ({"<revision>": {"<METHOD> <path>": [code, body]}}): answers follow the
# revision that serves the service URL (100%) or carries the tag of a tag URL, so a test sees
# what the code of the serving revision would answer. Without an entry: state["http"].
answers = state["http"].get(base_url, {})
for svc in state["services"].values():
    for entry in svc["traffic"]:
        rev = entry.get("revisionName")
        if entry.get("tag") and entry.get("url") == base_url and rev in state.get("http_rev", {}):
            answers = state["http_rev"][rev]
        elif not entry.get("tag") and svc["url"] == base_url and entry.get("percent") == 100 \
                and rev in state.get("http_rev", {}):
            answers = state["http_rev"][rev]
code, body = answers.get(f"{method} {path}", [404, '{"detail":"Not Found"}'])
if isinstance(body, (dict, list)):
    body = json.dumps(body)
if data is not None:
    with open(here / "curl-bodies.log", "a") as log:
        log.write(json.dumps({"url": url, "data": data}) + "\n")
if out and out != "/dev/null":
    Path(out).write_text(body)
elif not out:
    sys.stdout.write(body)
sys.stdout.write(fmt.replace("%{http_code}", str(code)).replace("%{time_total}", "0.050"))
'''

PROBE_OK = {"status": "ok", "endpoint": "x"}
ITEMS = [{"id": f"item-10{i}", "name": f"Item {i}", "stock": 1000, "price_cents": 1} for i in range(1, 6)]


def healthy_http(base):
    return {
        "GET /health": [200, {"status": "ok"}],
        "GET /items": [200, ITEMS],
        "GET /admin/leak-connections": [200, PROBE_OK],
        "GET /admin/fault/inject": [200, PROBE_OK],
        "GET /admin/restock": [200, PROBE_OK],
    }


def without(answers, *keys):
    """A copy of HTTP answers without the given "<METHOD> <path>" entries (404 then)."""
    return {k: v for k, v in answers.items() if k not in keys}


def for_prefix(state, prefix):
    """The same state for another NAME_PREFIX (every 'ata-' name becomes '<prefix>-')."""
    return json.loads(json.dumps(state).replace("ata-", f"{prefix}-"))


def live_like_state():
    """Today's live shape: legacy pin 00073-9vb (maxScale 100, aliases) serving by name,
    template maxScale 100 with aliases, service-level max 100 on both services."""
    revisions = {
        OLD_PIN: {"service": INV, "image": f"{IMAGE_BASE}@{OLD_DIGEST}", "digest": OLD_DIGEST,
                  "env": dict(LEGACY_ENV), "max": "100", "min": "0", "ready": True,
                  "created": "2026-10-23T10:00:00Z"},
        "ata-orders-api-00004-rgg": {"service": ORD, "image": "orders@sha256:" + "cd" * 32,
                                     "digest": "sha256:" + "cd" * 32, "env": {}, "max": "100",
                                     "min": "0", "ready": True, "created": "2026-10-26T10:00:00Z"},
    }
    return {
        "services": {
            INV: {"url": INV_URL, "traffic": [{"revisionName": OLD_PIN, "percent": 100}],
                  "template": {"image": f"{IMAGE_BASE}@{OLD_DIGEST}", "env": dict(LEGACY_ENV),
                               "max": "100", "min": "0"},
                  "service_max": "100", "latest": "ata-inventory-api-00146-p5d"},
            ORD: {"url": ORD_URL,
                  "traffic": [{"revisionName": "ata-orders-api-00004-rgg", "percent": 100, "latestRevision": True}],
                  "template": {"image": "orders@sha256:" + "cd" * 32, "env": {}, "max": "100", "min": "0"},
                  "service_max": "100", "latest": "ata-orders-api-00004-rgg"},
        },
        "revisions": revisions,
        "counter": 146,
        "build_digest": NEW_DIGEST,
        "fail": [],
        "http": {INV_URL: healthy_http(INV_URL), ORD_URL: healthy_http(ORD_URL),
                 "https://repin-candidate---inv.example": healthy_http("candidate")},
    }


def fresh_project_state():
    """A fresh project: inventory deployed by Terraform/deploy.sh, serving its latest
    revision; the repo's pin (00073-9vb) is not a revision here."""
    state = live_like_state()
    first = "ata-inventory-api-00001-abc"
    del state["revisions"][OLD_PIN]
    state["revisions"][first] = {"service": INV, "image": f"{IMAGE_BASE}@{NEW_DIGEST}", "digest": NEW_DIGEST,
                                 "env": dict(BASELINE_ENV), "max": "1", "min": "0", "ready": True,
                                 "created": "2026-10-28T00:00:00Z"}
    state["services"][INV]["traffic"] = [{"revisionName": first, "percent": 100, "latestRevision": True}]
    state["services"][INV]["template"]["env"] = dict(BASELINE_ENV)
    state["services"][INV]["latest"] = first
    state["counter"] = 1
    return state


def write_pin(path, rev, digest, prev_rev="", prev_digest=""):
    path.write_text(
        "# test pin\n"
        f"PIN_INVENTORY_REVISION={rev}\nPIN_INVENTORY_DIGEST={digest}\n"
        f"PIN_INVENTORY_PREVIOUS_REVISION={prev_rev}\nPIN_INVENTORY_PREVIOUS_DIGEST={prev_digest}\n"
    )


class FakeCloud:
    def __init__(self, tmp_path, state):
        self.tmp_path = tmp_path
        self.dir = tmp_path / "bin"
        self.dir.mkdir()
        (self.dir / "state.json").write_text(json.dumps(state))
        for name, src in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
            (self.dir / f"{name}.py").write_text(src)
            exe = self.dir / name
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{self.dir}/{name}.py" "$@"\n')
            exe.chmod(0o755)
        self.pin_file = tmp_path / "inventory-pin.ata.env"
        write_pin(self.pin_file, OLD_PIN, OLD_DIGEST)

    def run(self, argv, **extra_env):
        env = {k: v for k, v in os.environ.items()
               if (k == "ATA_STACK_DIR" or not k.startswith(("PINNED_INVENTORY_", "ATA_", "GCP_"))) and k not in (
                   "PROJECT_ID", "REGION", "NAME_PREFIX", "MOCK_MODE", "DRY_RUN")}
        # The stack identity is required (common.sh has no defaults): this fake's stack.
        env.update(PATH=f"{self.dir}{os.pathsep}{env['PATH']}", ATA_INVENTORY_PIN_FILE=str(self.pin_file),
                   TMPDIR=str(self.tmp_path), PROJECT_ID=PROJECT, REGION="us-west1", NAME_PREFIX="ata")
        env.update(extra_env)
        return subprocess.run([TEST_BASH, *map(str, argv)], cwd=str(self.tmp_path), capture_output=True,
                              text=True, env=env, check=False, timeout=60)

    @property
    def state(self):
        return json.loads((self.dir / "state.json").read_text())

    def set_state(self, state):
        (self.dir / "state.json").write_text(json.dumps(state))

    def calls(self, prefix=None):
        log = self.dir / "calls.log"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [c for c in calls if prefix is None or c[:len(prefix)] == prefix]

    def curl_calls(self):
        log = self.dir / "curl.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def curl_bodies(self):
        log = self.dir / "curl-bodies.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def serving(self, svc=INV):
        return [(e["revisionName"], e["percent"]) for e in self.state["services"][svc]["traffic"]
                if (e.get("percent") or 0) > 0]

    def tags(self, svc=INV):
        return [e["tag"] for e in self.state["services"][svc]["traffic"] if e.get("tag")]
