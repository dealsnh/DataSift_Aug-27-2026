"""
Resolve a property address for Orange County CA "NT TRUSTEE SALE" (foreclosure)
records pulled by occa_recorder_pull.py.

WHY THIS IS NEEDED: confirmed live 2026-09-03 (see CLAUDE.md, "Orange County CA
source map") that the Recorder's own per-record detail view carries NO property
address, APN, or legal description -- only document metadata (page count,
recording date, document type) and the Grantor/Grantee NAMES. The only way to see
the actual legal description is to purchase the scanned document image via the
site's cart/checkout flow (real per-document cost). So the same Enformion
address-resolution pattern already proven for OC probate (`occa_address_resolve.py`)
is reused here: resolve the GRANTOR's name to their most recent Orange County, CA
street address.

THE GRANTOR BLOCK MIXES TWO DIFFERENT ROLES WITH NO FIELD TO TELL THEM APART.
A trustee-sale filing lists both the foreclosure TRUSTEE COMPANY (e.g. "CLEAR
RECON CORP TR") and the actual property owner/borrower (e.g. "LESTER SUZANNE") as
"Grantor" -- same field, same formatting. `split_grantors()` filters out business
names via entity-marker keywords (LLC, INC, CORP, SERVICES, TITLE, DEFAULT, ...)
and a lone individual owner may still legitimately carry a trailing "TR" of their
own (they hold title via a personal living trust, not the sale trustee) -- that
token is stripped, not treated as disqualifying.

CONFIDENCE, mirrors occa_address_resolve.py's spirit but the signals differ
(no date-of-death, no personal-representative graph -- this isn't probate):
  - high:   2+ distinct owner-candidate names on the SAME record resolve to the
            SAME Orange County address (household corroboration -- the same
            pattern that rescued spouse matches in the Ohio SOI pipeline).
  - medium: exactly one owner-candidate name, and it is the ONLY same-named
            person Enformion returns with an Orange County address (unambiguous).
  - low:    exactly one owner-candidate resolves, but multiple same-named OC
            people exist (a coin flip which one is the actual borrower).
  - unresolved: no owner-candidate name at all (fully entity-owned -- an LLC/
            trust bought the property, needs a BusinessV2 lookup, out of scope
            here), or no candidate resolves to any OC CA address.

Usage:
    python src/occa_foreclosure_address_resolve.py --in output/occa_foreclosure_pull_20260903.json
    python src/occa_foreclosure_address_resolve.py --limit 10 --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import enformion_heir as eh  # noqa: E402
from occa_address_resolve import _is_oc_ca, pick_address, _addr_recency  # noqa: E402

TARGET_STATE = "CA"

_ENTITY_MARKERS = {
    "LLC", "INC", "CORP", "CORPORATION", "COMPANY", "LLP", "LP", "CO",
    "SERVICES", "SOLUTIONS", "SPECIALISTS", "RECOVERY", "RECONVEYANCE",
    "DEFAULT", "TITLE", "INSURANCE", "LENDER", "LENDERS", "LENDING",
    "MANAGEMENT", "ADVISORS", "FORECLOSURE", "BANK", "NATIONAL", "MORTGAGE",
    "FINANCIAL", "CAPITAL", "FUND", "ASSOCIATION", "AGENCY", "GROUP",
    "HOLDINGS", "INVESTMENT", "SERVICING", "TRUSTEE", "ATTORNEY", "LEGAL",
    "LAW", "REAL", "ESTATE", "SYSTEMS", "PARTNERS", "PROGRESSIVE", "WESTERN",
    "NATIONWIDE", "WORLDWIDE",
}
_SUFFIX_DROP = {"TR", "TRUSTEE"}


def is_entity_name(name: str) -> bool:
    toks = set(re.findall(r"[A-Z']+", name.upper()))
    if "&" in name:
        return True
    return bool(toks & _ENTITY_MARKERS)


def split_grantors(grantors: list[str]) -> tuple[list[str], list[str]]:
    """(owner_candidate_names, entity_names). Strips a lone trailing TR/TRUSTEE
    token off an otherwise-individual name (a personal living trust marker, not
    the foreclosure trustee)."""
    owners, entities = [], []
    for g in grantors:
        g = (g or "").strip()
        if not g:
            continue
        if is_entity_name(g):
            entities.append(g)
            continue
        toks = g.split()
        if toks and toks[-1].upper().rstrip(".") in _SUFFIX_DROP:
            toks = toks[:-1]
        cleaned = " ".join(toks).strip()
        if cleaned:
            owners.append(cleaned)
    return owners, entities


def _split_name(full: str) -> tuple[str, str]:
    """Recorder indexes as 'LAST FIRST [MIDDLE]'. 'GUERRERO GERMAN' -> last
    Guerrero, first German. This is the OPPOSITE convention from a notice body,
    but matches every real grantor name observed in the live pull."""
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    last = parts[0]
    first = parts[1]
    return first, last


def resolve_owner(name: str, per_page: int = 10) -> tuple[dict | None, dict | None, int]:
    """One owner-candidate name -> (best matching person, their OC address, how
    many same-named OC-addressed people Enformion returned)."""
    first, last = _split_name(name)
    if not first or not last:
        return None, None, 0
    res = eh.person_search(first, last, state=TARGET_STATE, results_per_page=per_page)
    persons = res.get("persons") or []
    oc_matches = []
    for p in persons:
        a = pick_address(p)
        if a:
            oc_matches.append((p, a))
    if not oc_matches:
        return None, None, 0
    oc_matches.sort(key=lambda t: _addr_recency(t[1]), reverse=True)
    best_person, best_addr = oc_matches[0]
    return best_person, best_addr, len(oc_matches)


def resolve_record(rec: dict, verbose: bool = True) -> dict:
    out = dict(rec)
    out.update({
        "owner_candidates": [], "entity_names": [], "resolved_address": "",
        "resolved_city": "", "resolved_zip": "", "resolve_confidence": "unresolved",
        "resolve_signals": "", "resolved_via_name": "", "must_verify": "",
    })
    owners, entities = split_grantors(rec.get("grantors") or [])
    out["owner_candidates"] = owners
    out["entity_names"] = entities

    if not owners:
        out["resolve_signals"] = (
            "no individual owner name on this record (entity-owned) -- "
            "needs a BusinessV2 lookup, not covered by this pass"
        )
        if verbose:
            print(f"    {rec.get('doc','')[:16]:16} -> entity-owned, no person to search")
        return out

    resolved = []   # (name, person, addr, oc_matches)
    for name in owners:
        person, addr, n_oc = resolve_owner(name)
        resolved.append((name, person, addr, n_oc))
        time.sleep(0.2)

    with_addr = [(n, p, a, k) for n, p, a, k in resolved if a]
    if not with_addr:
        out["resolve_signals"] = (
            f"none of {len(owners)} owner name(s) resolved to an Orange County CA address"
        )
        if verbose:
            print(f"    {rec.get('doc','')[:16]:16} -> no OC address for "
                  f"{'; '.join(owners)}")
        return out

    # household corroboration: 2+ distinct candidate names landing on the same address
    by_addr: dict[str, list] = {}
    for n, p, a, k in with_addr:
        key = re.sub(r"[^a-z0-9]", "", (a.get("fullAddress") or "").lower())
        by_addr.setdefault(key, []).append((n, p, a, k))

    best_key = max(by_addr, key=lambda k: len(by_addr[k]))
    group = by_addr[best_key]
    chosen_addr = group[0][2]
    used_names = [g[0] for g in group]

    if len(group) >= 2:
        conf = "high"
        sig = f"{len(group)} owner names on this record independently resolved to the same address"
    else:
        n_oc = group[0][3]
        if n_oc == 1:
            conf = "medium"
            sig = "single owner name, unambiguous match (only one same-named OC resident)"
        else:
            conf = "low"
            sig = f"single owner name, but {n_oc} same-named OC residents -- may be the wrong person"

    full = chosen_addr.get("fullAddress") or ""
    # Enformion's own format is "STREET[, UNIT]; CITY, STATE ZIP" (semicolon
    # before the city, not a comma) -- e.g. "29552 Silverado Canyon Rd; Silverado,
    # CA 92676". A comma-based split (the first version here) matches at the
    # comma before an apartment/unit instead and glues the unit onto the city
    # ("Apt B; Santa Ana"), which then 403s the Zillow property-type lookup on a
    # garbage address. Match on the semicolon, per occa_address_resolve.py's
    # already-proven pattern for this exact API.
    m = re.match(r"\s*(.+?);\s*(.+?),\s*([A-Z]{2})\s*(\d{5})", full)
    out["resolved_address"] = m.group(1).strip() if m else full
    out["resolved_city"] = m.group(2).strip() if m else chosen_addr.get("city", "")
    out["resolved_zip"] = m.group(4) if m else chosen_addr.get("zip", "")
    out["resolve_confidence"] = conf
    out["resolve_signals"] = sig
    out["resolved_via_name"] = "; ".join(used_names)
    out["address_last_reported"] = chosen_addr.get("lastReportedDate", "")
    out["must_verify"] = (
        "Address resolved via Enformion person search on the grantor name, NOT "
        "read off the recorded document (which is paywalled behind a per-page "
        "purchase). Confirm against the Recorder image or a skip-trace hit "
        "before mailing."
    )
    if verbose:
        print(f"    {rec.get('doc','')[:16]:16} -> {out['resolved_address']}, "
              f"{out['resolved_city']}  [{conf}]")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                     help="just show the owner/entity split, no Enformion calls")
    args = ap.parse_args()

    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    recs = data["records"]
    if args.limit:
        recs = recs[:args.limit]

    if args.dry_run:
        n_owned, n_entity_only = 0, 0
        for r in recs:
            owners, entities = split_grantors(r.get("grantors") or [])
            tag = "OWNER" if owners else "ENTITY-ONLY"
            if owners:
                n_owned += 1
            else:
                n_entity_only += 1
            print(f"  {r['doc']:16} [{tag:11}] owners={owners} entities={entities}")
        print(f"\n{n_owned} record(s) with an individual owner name, "
              f"{n_entity_only} entity-only (no person to search)")
        return 0

    print(f"Resolving {len(recs)} record(s) via Enformion Person Search "
          f"(billed per match, misses free) ...")
    out_recs = []
    for i, r in enumerate(recs, 1):
        out_recs.append(resolve_record(r))
        if i % 10 == 0:
            print(f"  ... {i}/{len(recs)}", flush=True)

    out = args.out or args.inp.replace(".json", "_resolved.json")
    Path(out).write_text(json.dumps({
        **{k: v for k, v in data.items() if k != "records"},
        "resolved_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(out_recs),
        "records": out_recs,
    }, indent=2), encoding="utf-8")

    from collections import Counter
    cc = Counter(r["resolve_confidence"] for r in out_recs)
    print(f"\nwrote {len(out_recs)} record(s) -> {out}")
    print(f"confidence mix: {dict(cc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
