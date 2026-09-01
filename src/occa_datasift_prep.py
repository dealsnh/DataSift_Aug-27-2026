"""
Turn the resolved Orange County probate file into a DataSift upload CSV.

Tagging deliberately mirrors `datasift_formatter._build_tags()` rather than being
hand-written. A one-off upload that tags from memory silently drifts from the
account's convention -- that is exactly how the Sunada record ended up missing
`FTM` / `deceased` and carrying a non-canonical `Orange County` tag that could not
then be removed (DataSift accumulates tags on upsert and exposes no delete).

Lists: the canonical type list ONLY (`Probate`). No dated per-pull list -- a record
parked in one never enters the niche sequential funnel, which keys off the type
lists. Batch traceability comes from the `pulled_<date>` tag and the Drive filename.

The contact on a probate record is the PERSONAL REPRESENTATIVE, never the decedent.

Usage:
    python src/occa_datasift_prep.py --in output/occa_probate_resolved_20260902.json
    python src/occa_datasift_prep.py --min-confidence medium
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

COLUMNS = [
    "Property Street Address", "Property City", "Property State", "Property ZIP Code",
    "Owner First Name", "Owner Last Name",
    "Mailing Street Address", "Mailing City", "Mailing State", "Mailing ZIP Code",
    "Lists", "Tags", "Notes",
    "Notice Type", "County", "Source URL",
    "Personal Representative", "Decedent Name",
    "Probate Open Date",
]

CANONICAL_LIST = "Probate"
RANK = {"high": 3, "medium": 2, "low": 1, "unresolved": 0, "error": 0}


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"[\s,]+", (full or "").strip()) if p]
    parts = [p for p in parts
             if p.strip(".").upper() not in {"JR", "SR", "II", "III", "IV", "ESQ"}]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def build_tags(rec: dict, pulled: str) -> str:
    """Mirror datasift_formatter._build_tags for a probate record."""
    tags = ["Courthouse Data", "FTM", "probate"]

    county = (rec.get("county") or "").strip().lower()
    if county:
        tags.append(county)

    src = rec.get("date_published") or ""
    try:
        tags.append(datetime.strptime(src, "%Y-%m-%d").strftime("%Y-%m"))
    except ValueError:
        pass

    # A probate notice means the owner is dead by definition.
    tags.append("deceased")

    conf = (rec.get("resolve_confidence") or "").lower()
    # The address is a research result, not a fact off the notice. Carry its
    # confidence INTO the CRM so a weak match is filterable rather than
    # indistinguishable from a confirmed one once it is sitting in a list.
    if conf in ("high", "medium"):
        tags.append(f"address_{conf}_confidence")
    else:
        tags.append("address_low_confidence")
    if (rec.get("ambiguous") or "") == "yes":
        tags.append("address_needs_verify")

    tags.append(f"pulled_{pulled}")
    return ",".join(tags)


def build_notes(rec: dict) -> str:
    bits = []
    if rec.get("decedent_name"):
        bits.append(f"Decedent: {rec['decedent_name']}")
    if rec.get("case_number"):
        bits.append(f"OC Superior Court case: {rec['case_number']}")
    if rec.get("hearing_date"):
        bits.append(f"Hearing: {rec['hearing_date']}")
    if rec.get("attorney"):
        bits.append(f"Attorney for petitioner: {rec['attorney']}")
    if rec.get("source_paper"):
        bits.append(f"Published in {rec['source_paper']} on {rec.get('date_published','')}")
    if rec.get("resolved_address"):
        bits.append(
            f"Address source: {rec.get('resolve_source','')} "
            f"(confidence {rec.get('resolve_confidence','')}, "
            f"last reported {rec.get('address_last_reported','')}). "
            f"Evidence: {rec.get('resolve_signals','')}")
    if rec.get("must_verify"):
        bits.append(f"MUST VERIFY: {rec['must_verify']}")
    return " | ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp",
                    default="output/occa_probate_resolved_20260902.json")
    ap.add_argument("--out", default="")
    ap.add_argument("--min-confidence", default="low",
                    choices=["high", "medium", "low"],
                    help="lowest address confidence to include (default low = all "
                         "records that resolved to an address)")
    args = ap.parse_args()

    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    recs = data["records"]
    floor = RANK[args.min_confidence]
    pulled = datetime.now().strftime("%Y-%m-%d")

    rows, skipped = [], []
    for r in recs:
        if not (r.get("resolved_address") or "").strip():
            skipped.append((r.get("decedent_name"), "no property address"))
            continue
        if RANK.get((r.get("resolve_confidence") or "").lower(), 0) < floor:
            skipped.append((r.get("decedent_name"),
                            f"below {args.min_confidence} confidence"))
            continue
        pr = r.get("personal_representative") or ""
        first, last = split_name(pr)
        if not first:
            skipped.append((r.get("decedent_name"), "no personal representative"))
            continue
        rows.append({
            "Property Street Address": r["resolved_address"],
            "Property City": r.get("resolved_city", ""),
            "Property State": "CA",
            "Property ZIP Code": r.get("resolved_zip", ""),
            "Owner First Name": first,
            "Owner Last Name": last,
            # No separate PR mailing address is published in a CA probate notice,
            # so mailing intentionally falls back to the property address rather
            # than inventing the attorney's office as the owner's mail drop.
            "Mailing Street Address": "",
            "Mailing City": "", "Mailing State": "", "Mailing ZIP Code": "",
            "Lists": CANONICAL_LIST,
            "Tags": build_tags(r, pulled),
            "Notes": build_notes(r),
            "Notice Type": "probate",
            "County": r.get("county", "Orange"),
            "Source URL": r.get("source_url", ""),
            "Personal Representative": pr,
            "Decedent Name": r.get("decedent_name", ""),
            "Probate Open Date": r.get("date_published", ""),
        })

    out = args.out or f"output/occa_datasift_upload_{datetime.now():%Y%m%d}.csv"
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
        for name, why in skipped:
            print(f"    {name[:34]:34} {why}")
    if rows:
        print("\n  sample row tags:")
        print(f"    {rows[0]['Tags']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
