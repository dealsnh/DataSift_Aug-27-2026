"""
Turn the resolved + property-type-filtered Orange County foreclosure file into a
DataSift upload CSV. Mirrors `occa_datasift_prep.py`'s probate version -- same
tag convention (`datasift_formatter._build_tags()`-style), same "canonical list
only, no dated per-pull list" rule, same reason: a one-off upload that tags from
memory silently drifts from the account's convention.

List: the canonical type list ONLY (`Foreclosure`) -- these are all recorded
"NT TRUSTEE SALE" filings, i.e. an active, already-scheduled trustee sale, not a
Notice of Default (which would be `Pre-Foreclosure`). Batch traceability comes
from the `pulled_<date>` tag and the Drive filename, same as every other pull.

The contact on a foreclosure record is the OWNER (the person being foreclosed
on), never the trustee company -- `resolved_via_name` already carries only
owner-candidate name(s), the trustee/company names sit in `entity_names` for
Notes context only.

Usage:
    python src/occa_foreclosure_datasift_prep.py --in output/occa_foreclosure_pull_20260903_filtered.json
"""
from __future__ import annotations

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

COLUMNS = [
    "Property Street Address", "Property City", "Property State", "Property ZIP Code",
    "Owner First Name", "Owner Last Name",
    "Mailing Street Address", "Mailing City", "Mailing State", "Mailing ZIP Code",
    "Lists", "Tags", "Notes",
    "Notice Type", "County", "Source URL",
]

CANONICAL_LIST = "Foreclosure"
RECORDER_URL = "https://cr.occlerkrecorder.gov/RecorderWorksInternet/"
RANK = {"high": 3, "medium": 2, "low": 1, "unresolved": 0}


def split_owner_name(via: str) -> tuple[str, str]:
    """resolved_via_name carries the owner-candidate name(s) exactly as they came
    off the Recorder's own Grantor field -- 'LAST FIRST [MIDDLE]', the SAME
    convention `occa_foreclosure_address_resolve.py._split_name` already had to
    account for (its own docstring: "GUERRERO GERMAN" -> last Guerrero, first
    German). Splitting this as if it were "FIRST LAST" swaps every name on the
    upload -- caught live on the first verification record (2026000247324:
    First/Last briefly went out as Guerrero/German instead of German/Guerrero).
    For a household match ('LAST1 FIRST1; LAST2 FIRST2') the first person is the
    CRM contact."""
    first_person = (via or "").split(";")[0].strip()
    parts = [p for p in re.split(r"\s+", first_person) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    last, first = parts[0], " ".join(parts[1:])
    return first, last


def build_tags(rec: dict, pulled: str) -> str:
    tags = ["Courthouse Data", "FTM", "foreclosure", "orange"]

    try:
        tags.append(datetime.strptime(rec.get("date", ""), "%m/%d/%Y").strftime("%Y-%m"))
    except ValueError:
        pass

    conf = (rec.get("resolve_confidence") or "").lower()
    tags.append(f"address_{conf}_confidence" if conf in ("high", "medium") else "address_low_confidence")
    if conf == "low":
        tags.append("address_needs_verify")

    if rec.get("property_type"):
        tags.append(re.sub(r"[^a-z]+", "_", rec["property_type"].lower()).strip("_"))

    tags.append(f"pulled_{pulled}")
    return ",".join(tags)


def build_notes(rec: dict) -> str:
    bits = [
        f"OC Clerk-Recorder Document Number: {rec.get('doc', '')}",
        f"Recorded: {rec.get('date', '')}",
        f"Document Type: NT TRUSTEE SALE (code 210)",
    ]
    if rec.get("resolved_via_name"):
        bits.append(f"Owner (via Enformion): {rec['resolved_via_name']}")
    if rec.get("entity_names"):
        bits.append(f"Trustee/other party on filing: {'; '.join(rec['entity_names'])}")
    if rec.get("property_type"):
        bits.append(f"Property type (Zillow): {rec['property_type']}")
    if rec.get("resolve_signals"):
        bits.append(f"Address confidence basis: {rec['resolve_signals']}")
    if rec.get("address_last_reported"):
        bits.append(f"Address last reported: {rec['address_last_reported']}")
    if rec.get("must_verify"):
        bits.append(f"MUST VERIFY: {rec['must_verify']}")
    bits.append(
        "No auction/sale date captured -- the Recorder's index carries only the "
        "recording date; the actual sale date is on the scanned document image "
        "(paywalled). Confirm the sale hasn't already occurred before outreach."
    )
    return " | ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--min-confidence", default="low", choices=["high", "medium", "low"])
    args = ap.parse_args()

    import json
    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    recs = data["records"]
    floor = RANK[args.min_confidence]
    pulled = datetime.now().strftime("%Y-%m-%d")

    rows, skipped = [], []
    for r in recs:
        if not (r.get("resolved_address") or "").strip():
            skipped.append((r.get("doc"), "no resolved address"))
            continue
        if RANK.get((r.get("resolve_confidence") or "").lower(), 0) < floor:
            skipped.append((r.get("doc"), f"below {args.min_confidence} confidence"))
            continue
        first, last = split_owner_name(r.get("resolved_via_name", ""))
        if not first:
            skipped.append((r.get("doc"), "no owner name to split"))
            continue
        rows.append({
            "Property Street Address": r["resolved_address"],
            "Property City": r.get("resolved_city", ""),
            "Property State": "CA",
            "Property ZIP Code": r.get("resolved_zip", ""),
            "Owner First Name": first,
            "Owner Last Name": last,
            "Mailing Street Address": "",
            "Mailing City": "", "Mailing State": "", "Mailing ZIP Code": "",
            "Lists": CANONICAL_LIST,
            "Tags": build_tags(r, pulled),
            "Notes": build_notes(r),
            "Notice Type": "foreclosure",
            "County": "Orange",
            "Source URL": RECORDER_URL,
        })

    out = args.out or f"output/occa_foreclosure_upload_{datetime.now():%Y%m%d}.csv"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"prepared {len(rows)} record(s) for DataSift  ->  {out}")
    print(f"  list  : {CANONICAL_LIST}")
    from collections import Counter
    cc = Counter(r.get("resolve_confidence") for r in recs if r.get("resolved_address"))
    print(f"  confidence mix of included: {dict(cc)}")
    if skipped:
        print(f"\n  skipped {len(skipped)}:")
        for doc, why in skipped:
            print(f"    {doc:16} {why}")
    if rows:
        print("\n  sample row tags:")
        print(f"    {rows[0]['Tags']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
