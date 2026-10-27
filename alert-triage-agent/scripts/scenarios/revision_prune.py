#!/usr/bin/env python3
"""Select Cloud Run revisions to prune (read-only; used by scripts/prune-revisions.sh).

  revision_prune.py --service-json FILE --revisions-json FILE [--protect REV]...

FILE contents come from `gcloud run services describe --format=json` and
`gcloud run revisions list --service=... --format=json`. Prints one line per revision,
oldest first, as "<KEEP|DELETE> <name> <created> <reason>":

- KEEP: any revision named in status.traffic (serving, or tagged), the latest created and
  latest ready revisions, and every --protect revision (the pinned revision, the previous
  pin, --keep). A --protect revision that does not exist is reported as
  "MISSING <name> - protected" so the caller can decide (it is never deleted either way).
- DELETE: every other revision of the service (0% traffic, untagged, not latest, not
  protected).

Exit 2 on unreadable input; a revision of another service in the list is an input error.
"""

import argparse
import json
import sys


def select(service, revisions, protect):
    name = (service.get("metadata") or {}).get("name") or ""
    status = service.get("status") or {}
    reasons = {}
    for t in status.get("traffic") or []:
        rev = t.get("revisionName")
        if not rev:
            continue
        if (t.get("percent") or 0) > 0:
            reasons.setdefault(rev, f"serves {t.get('percent')}%")
        elif t.get("tag"):
            reasons.setdefault(rev, f"traffic tag {t.get('tag')}")
    for key, why in (("latestCreatedRevisionName", "latest created"), ("latestReadyRevisionName", "latest ready")):
        if status.get(key):
            reasons.setdefault(status[key], why)
    for rev in protect:
        reasons.setdefault(rev, "protected (pin / previous pin / --keep)")

    rows, seen = [], set()
    for doc in revisions:
        meta = doc.get("metadata") or {}
        rev = meta["name"]
        owner = (meta.get("labels") or {}).get("serving.knative.dev/service")
        if owner and name and owner != name:
            raise ValueError(f"revision {rev} belongs to {owner}, not {name}")
        seen.add(rev)
        created = meta.get("creationTimestamp") or "-"
        rows.append((created, "KEEP" if rev in reasons else "DELETE", rev, reasons.get(rev, "0% traffic, untagged")))
    rows.sort()
    out = [f"{action} {rev} {created} {why}" for created, action, rev, why in rows]
    out += [f"MISSING {rev} - protected" for rev in protect if rev not in seen]
    return out


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--service-json", required=True)
    p.add_argument("--revisions-json", required=True)
    p.add_argument("--protect", action="append", default=[])
    args = p.parse_args(argv)
    try:
        with open(args.service_json) as f:
            service = json.load(f)
        with open(args.revisions_json) as f:
            revisions = json.load(f)
        lines = select(service, revisions, [r for r in args.protect if r])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"revision_prune: unreadable input: {exc!r}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
