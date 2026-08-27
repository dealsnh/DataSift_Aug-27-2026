"""Create the DataSift custom-field schema for the .env-authenticated account.

datasift_api_upload.py mints its own JWT from DATASIFT_EMAIL / DATASIFT_PASSWORD
in .env -- whatever DataSift login that is, not necessarily the account
datasift_schema_setup.py targets (which reads a shared credential store on a
different machine entirely, C:\\Users\\Tyrus\\...\\_api, and won't even import
if that checkout isn't present locally).

Discovered 2026-08-28, testing a first-to-market pull for Orange County CA: on
a fresh .env account the custom-fields endpoint returned 0 results and every
FIELD_MAP lookup in datasift_api_upload.py silently skipped with a warning --
not a bug, just an account that had never had schema created in it.

This creates the 5 fields datasift_api_upload.FIELD_MAP needs for a first-to-
market pull outside the lien/tax-delinquency Knox fields (those live in
datasift_schema_setup.py's NEW_FIELDS and can be layered in here the same way
if a future pull needs them): Notice Type (select, the 7 canonical types,
lowercase -- matches knox_ftm_pull.py's actual notice_type casing), County,
Source URL, Personal Representative, Decedent Name (all plain text, so any
county name or free-text PR/decedent works with no option list to maintain).

Idempotent and dry-run by default, same UX as datasift_schema_setup.py.

    python src/datasift_schema_bootstrap.py            # show the plan
    python src/datasift_schema_bootstrap.py --commit   # apply it
"""
from __future__ import annotations

import argparse

from datasift_api_upload import Api

GROUP_LABEL = "First-to-Market"

# The 7 canonical notice types from the first-market-county-data skill /
# foreclosure_filter.py. Lowercase to match what the real pipelines send.
NOTICE_TYPE_OPTIONS = ["foreclosure", "tax_sale", "tax_delinquent", "probate",
                       "eviction", "code_violation", "divorce"]

NEW_FIELDS = [
    ("Notice Type", "select"),
    ("County", "text"),
    ("Source URL", "text"),
    ("Personal Representative", "text"),
    ("Decedent Name", "text"),
]


def _ensure_group(api: Api, commit: bool) -> int | None:
    groups = api.call("/api/internal/custom-fields/group/?limit=999").get("results") or []
    for g in groups:
        if g.get("label") == GROUP_LABEL:
            return g["id"]
    if not commit:
        print("  GROUP CREATE  %s" % GROUP_LABEL)
        return None
    g = api.call("/api/internal/custom-fields/group/", "POST",
                 {"label": GROUP_LABEL, "entity_type": "property"})
    print("  group created  %s -> id=%s" % (GROUP_LABEL, g["id"]))
    return g["id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    tag = "COMMIT" if a.commit else "DRY RUN"
    api = Api()

    group_id = _ensure_group(api, a.commit)

    existing = api.call("/api/internal/custom-fields/?limit=999").get("results") or []
    by_label = {f.get("label"): f for f in existing}
    print("[%s] %d existing fields, group_id=%s\n" % (tag, len(existing), group_id))

    for label, ftype in NEW_FIELDS:
        if label in by_label:
            print("  exists   %-26s (%s)" % (label, by_label[label].get("field_type")))
            continue
        if not a.commit:
            print("  CREATE   %-26s %s" % (label, ftype))
            continue
        body = {"label": label, "field_type": ftype, "entity_type": "property",
                "group_id": group_id}
        if ftype in ("select", "multiselect"):
            opts = NOTICE_TYPE_OPTIONS if label == "Notice Type" else []
            body["options"] = [{"label": o, "value": o} for o in opts]
        r = api.call("/api/internal/custom-fields/", "POST", body)
        by_label[label] = r
        print("  created  %-26s %s -> %s" % (label, ftype, r.get("uuid") or r.get("id")))

    if not a.commit:
        print("\nNothing written. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
