#!/usr/bin/env python3
"""Check Cloud Run JSON (from `gcloud ... describe --format=json`, on stdin) against the
<prefix>-inventory-api / demo baseline. Used by common.sh, preflight.sh, repin-inventory.sh
and set-demo-instance-caps.sh. Read-only: it only parses what gcloud printed.

Subcommands (exit 0 = matches, 1 = differs, 2 = unreadable input or bad arguments; the
differences or the error go to stderr, one line):

  template  --image IMG --env K=V,... --absent K,... --max N
      The service's revision template (spec.template): image, env values, env names that
      must be absent (legacy aliases), autoscaling maxScale == N and minScale 0/unset.
  revision  --digest sha256:... --env K=V,... --absent K,... --max N
      One revision: status.imageDigest is the digest, the same env/scaling checks, and the
      Ready condition is True.
  service-max --max N
      The service-level instance limit (metadata annotation run.googleapis.com/maxScale,
      set with `gcloud run services update --max`) is N.
  scaling --kind template|revision --max N
      Only the instance limits (maxScale == N, minScale 0/unset) of the service's revision
      template, or of one revision. For <prefix>-orders-api, which has no pinned env baseline.
  traffic --expect REV
      The serving split (traffic entries with percent > 0) is exactly REV at 100%. Tag-only
      entries (percent 0, e.g. a re-pin candidate's tag) are ignored.
  serving
      Print the revisions with percent > 0, one "revision percent" per line.
  stock --expect ID=N,...
      Inventory JSON (GET /items list, or POST /admin/restock {"items": [...]}): every
      listed item exists with exactly that stock.
  tag-url --tag T
      Print the URL of traffic tag T (status.traffic[].url); exit 1 if the tag is absent.
"""

import argparse
import json
import sys

MAX_SCALE = "autoscaling.knative.dev/maxScale"
MIN_SCALE = "autoscaling.knative.dev/minScale"
SERVICE_MAX_SCALE = "run.googleapis.com/maxScale"


def _pairs(csv):
    if not csv:
        return {}
    out = {}
    for item in csv.split(","):
        if "=" not in item:
            raise ValueError(f"expected KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        out[key] = value
    return out


def _names(csv):
    return [n for n in (csv or "").split(",") if n]


def _container_diffs(container, annotations, env_wanted, absent, max_scale):
    env = {e.get("name"): e.get("value") for e in container.get("env") or []}
    diffs = [f"{k}={env.get(k)} (want {v})" for k, v in env_wanted.items() if env.get(k) != v]
    diffs += [f"{k} is set (legacy alias; must be absent)" for k in absent if k in env]
    return diffs + _scaling_diffs(annotations, max_scale)


def check_template(doc, args):
    template = doc["spec"]["template"]
    container = template["spec"]["containers"][0]
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    diffs = []
    if container.get("image") != args.image:
        diffs.append(f"image={container.get('image')} (want {args.image})")
    diffs += _container_diffs(container, annotations, _pairs(args.env), _names(args.absent), args.max)
    return diffs


def check_revision(doc, args):
    container = doc["spec"]["containers"][0]
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    status = doc.get("status") or {}
    diffs = []
    digest = (status.get("imageDigest") or "").rsplit("@", 1)[-1]
    if digest != args.digest:
        diffs.append(f"imageDigest={digest or None} (want {args.digest})")
    diffs += _container_diffs(container, annotations, _pairs(args.env), _names(args.absent), args.max)
    ready = [c for c in status.get("conditions") or [] if c.get("type") == "Ready"]
    if not ready or ready[0].get("status") != "True":
        diffs.append(f"Ready={ready[0].get('status') if ready else None} (want True)")
    return diffs


def _scaling_diffs(annotations, max_scale):
    diffs = []
    if annotations.get(MAX_SCALE) != str(max_scale):
        diffs.append(f"maxScale={annotations.get(MAX_SCALE)} (want {max_scale})")
    if annotations.get(MIN_SCALE) not in (None, "0"):
        diffs.append(f"minScale={annotations.get(MIN_SCALE)} (want 0)")
    return diffs


def check_scaling(doc, args):
    meta = doc["spec"]["template"].get("metadata") if args.kind == "template" else doc.get("metadata")
    return _scaling_diffs((meta or {}).get("annotations") or {}, args.max)


def _serving(doc):
    out = []
    for t in (doc.get("status") or {}).get("traffic") or []:
        if (t.get("percent") or 0) > 0:
            out.append((t.get("revisionName") or "", t.get("percent")))
    return out


def check_traffic(doc, args):
    serving = _serving(doc)
    if serving == [(args.expect, 100)]:
        return []
    shown = ", ".join(f"{r or '<latest>'}={p}" for r, p in serving) or "<none>"
    return [f"serving {shown} (want {args.expect}=100)"]


def check_stock(doc, args):
    items = doc["items"] if isinstance(doc, dict) else doc
    stock = {}
    for item in items:
        stock[item["id"]] = item.get("stock")
    diffs = []
    for item_id, want in _pairs(args.expect).items():
        if item_id not in stock:
            diffs.append(f"{item_id} missing")
        elif stock[item_id] != int(want):
            diffs.append(f"{item_id} stock={stock[item_id]} (want {want})")
    return diffs


def check_service_max(doc, args):
    live = ((doc.get("metadata") or {}).get("annotations") or {}).get(SERVICE_MAX_SCALE)
    return [] if live == str(args.max) else [f"service-level maxScale={live} (want {args.max})"]


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("template")
    t.add_argument("--image", required=True)
    r = sub.add_parser("revision")
    r.add_argument("--digest", required=True)
    for p in (t, r):
        p.add_argument("--env", required=True)
        p.add_argument("--absent", default="")
        p.add_argument("--max", type=int, required=True)
    s = sub.add_parser("service-max")
    s.add_argument("--max", type=int, required=True)
    sc = sub.add_parser("scaling")
    sc.add_argument("--kind", choices=["template", "revision"], required=True)
    sc.add_argument("--max", type=int, required=True)
    tr = sub.add_parser("traffic")
    tr.add_argument("--expect", required=True)
    sub.add_parser("serving")
    st = sub.add_parser("stock")
    st.add_argument("--expect", required=True)
    u = sub.add_parser("tag-url")
    u.add_argument("--tag", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    try:
        doc = json.loads(sys.stdin.read())
        if args.cmd == "tag-url":
            urls = [t.get("url") for t in (doc.get("status") or {}).get("traffic") or []
                    if t.get("tag") == args.tag and t.get("url")]
            if not urls:
                print(f"no traffic tag {args.tag!r} with a URL on the service", file=sys.stderr)
                return 1
            print(urls[0])
            return 0
        if args.cmd == "serving":
            for rev, pct in _serving(doc):
                print(f"{rev} {pct}")
            return 0
        check = {"template": check_template, "revision": check_revision,
                 "service-max": check_service_max, "scaling": check_scaling,
                 "traffic": check_traffic, "stock": check_stock}[args.cmd]
        diffs = check(doc, args)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        print(f"unreadable Cloud Run JSON for '{args.cmd}': {exc!r}", file=sys.stderr)
        return 2
    if diffs:
        print("differs from baseline: " + ", ".join(diffs), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
