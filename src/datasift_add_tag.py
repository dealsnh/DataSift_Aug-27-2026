"""
Add a tag to every property in an already-uploaded CSV.

Why this exists: `POST /api/internal/property/` does NOT upsert, so you cannot add a
tag by re-posting the row -- it just 400s "Property address already exists!". The
route that works on an existing record is
`POST /api/internal/property/<uuid>/add-tags/` with `{"tags": ["<title>"]}`, which
returns `{"new_tags": [...]}`. Tags ACCUMULATE and there is no remove endpoint, so
this resolves the tag against the account's existing tag list first and refuses an
unknown spelling unless --create is passed. A typo here is permanent.

Usage:
    python src/datasift_add_tag.py --csv output/occa_datasift_upload_20260902.csv \
        --tag "Priority 1"
    python src/datasift_add_tag.py --csv ... --tag "Priority 1" --commit
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datasift_api_upload import Api, BASE, build_property, _existing_uuid  # noqa: E402


def existing_tags(api: Api) -> dict:
    out, url = {}, "/api/internal/tag/?limit=500"
    d = api.call(url, "GET")
    items = d.get("results", d.get("data", [])) if isinstance(d, dict) else []
    for i in items:
        if isinstance(i, dict) and i.get("title"):
            out[i["title"].strip().lower()] = i["title"].strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--create", action="store_true",
                    help="allow a tag title that does not already exist in the account")
    ap.add_argument("--sleep", type=float, default=0.15)
    a = ap.parse_args()

    api = Api()
    known = existing_tags(api)
    canon = known.get(a.tag.strip().lower())
    if canon is None:
        if not a.create:
            print(f"Tag {a.tag!r} does not exist in this account.")
            near = [v for k, v in known.items() if a.tag.strip().lower()[:5] in k]
            if near:
                print(f"  did you mean: {near}")
            print("  Tags cannot be removed once applied. Re-run with --create if the "
                  "new spelling is intended.")
            return 2
        canon = a.tag.strip()
        print(f"tag {canon!r} will be CREATED")
    elif canon != a.tag.strip():
        print(f"matching existing tag spelling: {a.tag!r} -> {canon!r}")

    with open(a.csv, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(f"{len(rows)} record(s) from {a.csv}")
    print(f"tag: {canon!r}\n")

    if not a.commit:
        print("DRY RUN. Nothing written. Re-run with --commit.")
        return 0

    tagged = already = failed = 0
    errors = []
    for i, row in enumerate(rows, 1):
        addr = row.get("Property Street Address", "")[:34]
        try:
            # resolve the uuid: POST returns 201 with it, or 400 carrying the existing one
            try:
                res = api.call("/api/internal/property/", "POST", build_property(row))
                uuid = res.get("uuid") if isinstance(res, dict) else None
            except Exception as e:
                uuid = _existing_uuid(e)
                if not uuid:
                    raise
            res = api.call("/api/internal/property/%s/add-tags/" % uuid,
                           "POST", {"tags": [canon]})
            new = (res or {}).get("new_tags") or []
            if canon in new:
                tagged += 1
            else:
                already += 1
        except Exception as e:
            failed += 1
            errors.append("%s | %s" % (addr, str(e)[:140]))
            if len(errors) <= 5:
                print("  FAIL", errors[-1])
        if i % 10 == 0:
            print(f"   {i}/{len(rows)} tagged={tagged} already={already} failed={failed}",
                  flush=True)
        time.sleep(a.sleep)

    print(f"\ntagged={tagged}  already-had-it={already}  failed={failed}")

    # No batch notification here on purpose (Ty, 2026-09-03): this is a
    # follow-on retag of records step 3 already reported. Verify success in
    # the terminal counts above / --commit exit code instead of Google Chat.

    # Tagging nothing across a non-empty file is a failure, not a quiet success.
    return 1 if (rows and not (tagged + already)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
