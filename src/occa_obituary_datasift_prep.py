"""
Turn the resolved + property-type-filtered Orange County obituary file into a
DataSift upload CSV. Mirrors `occa_foreclosure_datasift_prep.py` -- same tag
convention, same canonical-list-only rule, same reason (a hand-tagged upload
silently drifts from the account's real convention).

List: the canonical `Obituary` list (confirmed present in the account, see
CLAUDE.md "Output CSVs auto-upload to Google Drive" section). Batch
traceability is tags (`pulled_<date>`), never a dated list.

The contact is the DECEDENT -- unlike foreclosure/probate there is no
separate owner-vs-trustee or owner-vs-PR split at this stage; the obituary
pull resolved the decedent's own most likely Orange County residence, and
`decedent_name` is already in normal "First [Middle] Last" order (not the
Recorder's reversed convention), so the split is the simple case.

EVERY RECORD FROM THIS PULL IS STRUCTURALLY LOW CONFIDENCE, and that is
expected, not a bug: an obituary carries no second name (no personal
representative, no co-owner) for the resolver to cross-check against
Enformion's relatives graph the way probate/foreclosure records can, so
`resolve_confidence` cannot climb past what venue-matching alone supports.
Every row gets `address_low_confidence` + `address_needs_verify` tags.

The nice-to-have owner-characteristic filters (free & clear / vacant /
senior owner / absentee owner) have NO automated source for Orange County
(see CLAUDE.md "Orange County CA property/tax-roll source map" -- the only
path found is a manual CPRA records request). They are not applied here;
every row's Notes says so explicitly rather than silently omitting them.

Usage:
    python src/occa_obituary_datasift_prep.py --in output/occa_obituary_filtered_20260911.json
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

CANONICAL_LIST = "Obituary"
RANK = {"high": 3, "medium": 2, "low": 1, "unresolved": 0}

_SUFFIX_DROP = {"jr", "sr", "ii", "iii", "iv", "v"}


def split_decedent_name(full: str) -> tuple[str, str]:
    """'Mark W. Erickson' -> ('Mark', 'Erickson'). Normal First..Last order
    (NOT the Recorder's reversed 'Last First' convention foreclosure uses) --
    an obituary byline/title is already in natural name order."""
    parts = [p.strip(".") for p in re.split(r"\s+", (full or "").strip()) if p.strip(".")]
    parts = [p for p in parts if p.lower() not in _SUFFIX_DROP]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def build_tags(rec: dict, pulled: str) -> list[str]:
    tags = ["Courthouse Data", "FTM", "obituary", "orange"]

    try:
        tags.append(datetime.strptime(rec.get("date_published", ""), "%m/%d/%Y")
                    .strftime("%Y-%m"))
    except ValueError:
        pass

    conf = (rec.get("resolve_confidence") or "").lower()
    tags.append(f"address_{conf}_confidence" if conf in ("high", "medium") else "address_low_confidence")
    if conf != "high":
        tags.append("address_needs_verify")

    if rec.get("property_type"):
        tags.append(re.sub(r"[^a-z]+", "_", rec["property_type"].lower()).strip("_"))

    tags.append(f"pulled_{pulled}")
    return tags


def build_notes(rec: dict) -> str:
    bits = [
        f"Obituary source: {rec.get('source_name', '')} -- {rec.get('source_url', '')}",
        f"Obituary published: {rec.get('date_published', '')}",
    ]
    if rec.get("resolved_via_name"):
        bits.append(f"Enformion match name: {rec['resolved_via_name']}")
    if rec.get("property_type"):
        bits.append(f"Property type (Zillow): {rec['property_type']}")
    if rec.get("resolve_signals"):
        bits.append(f"Address confidence basis: {rec['resolve_signals']}")
    if rec.get("oc_namesakes"):
        bits.append(f"{rec['oc_namesakes']} same-named people have an Orange County address "
                     f"on file -- no second name (PR/co-owner) exists this early to narrow it")
    if rec.get("address_last_reported"):
        bits.append(f"Address last reported: {rec['address_last_reported']}")
    if rec.get("must_verify"):
        bits.append(f"MUST VERIFY: {rec['must_verify']}")
    bits.append(
        "Free & clear / vacant / senior owner / absentee owner filters were NOT applied -- "
        "no automated Orange County source exists for these owner-characteristic flags "
        "(only path found is a manual CPRA public-records request, see CLAUDE.md)."
    )
    return " | ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    import json
    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    recs = data["records"]
    pulled = datetime.now().strftime("%Y-%m-%d")

    rows, skipped = [], []
    for r in recs:
        if not (r.get("resolved_address") or "").strip():
            skipped.append((r.get("decedent_name"), "no resolved address"))
            continue
        first, last = split_decedent_name(r.get("decedent_name", ""))
        if not first:
            skipped.append((r.get("decedent_name"), "no name to split"))
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
            "Tags": ",".join(build_tags(r, pulled)),
            "Notes": build_notes(r),
            "Notice Type": "obituary",
            "County": "Orange",
            "Source URL": r.get("source_url", ""),
        })

    out = args.out or f"output/occa_obituary_upload_{datetime.now():%Y%m%d}.csv"
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
        for who, why in skipped:
            print(f"    {who:24} {why}")
    if rows:
        print("\n  sample row tags:")
        print(f"    {rows[0]['Tags']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
