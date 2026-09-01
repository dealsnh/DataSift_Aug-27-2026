"""
Resolve the subject property address for Orange County CA probate records.

A CA probate notice never carries the decedent's property address -- the estate's
real property is only listed on the Inventory & Appraisal, filed after Letters are
granted. So the address has to be found from the outside.

WHY NOT THE ASSESSOR: California law forbids searching the Assessor by owner name,
and the OC Assessor withholds the owner name even on a known-APN lookup ("by law, we
do not allow searches by owner name; in addition, the name of the property owner is
not included in the search result"). The Knox County TN pattern that
`probate-property-finder` is built around simply does not transfer here.

WHAT WORKS: Enformion Person Search on the DECEDENT. Its address history is the same
route that independently confirmed 921 Van Ness Ct for the Sunada record in August.
We take the most recently reported Orange County address as the candidate property.

THIS PRODUCES A CANDIDATE, NOT A CONFIRMED SUBJECT PROPERTY. Address history proves
where a person lived, not what the estate owns or that they held title. Every row
carries a confidence and the signals behind it, and `must_verify` is never dropped.

Corroboration signals used (more signals = higher confidence):
  * the address sits in Orange County, which is where the estate is being probated
  * the decedent's surname matches the personal representative's (family estate)
  * the PR appears in the decedent's Enformion relatives graph
  * Enformion carries a date of death for the person
  * the name is unambiguous (exactly one person returned)

Usage:
    python src/occa_address_resolve.py --limit 15
    python src/occa_address_resolve.py --in output/occa_ftm_20260901.json --dry-run
"""
from __future__ import annotations

import argparse
import csv
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

TARGET_COUNTY = "orange"
TARGET_STATE = "CA"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _split_name(full: str) -> tuple[str, str]:
    """'DEBORAH SUE MEAN' -> ('DEBORAH', 'MEAN'). Middle names are dropped, and a
    trailing suffix is not allowed to become the surname."""
    parts = [p for p in re.split(r"[\s,]+", (full or "").strip()) if p]
    parts = [p for p in parts if p.strip(".").upper() not in
             {"JR", "SR", "II", "III", "IV", "V", "MD", "PHD", "ESQ", "DDS"}]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def name_variants(rec: dict) -> list[tuple[str, str]]:
    """Every (first, last) worth trying for one decedent, best guess first.

    Three real naming patterns in this data, each of which silently costs a match:
      * the notice publishes akas ("DEBORAH SUE MEAN, aka DEBORAH S. MEAD") and the
        aka is often the name the data broker actually indexed;
      * Hispanic naming puts the paternal surname in the middle slot, so
        "LUPE VASQUEZ MENDOZA" may be indexed under Vasquez;
      * a married woman's maiden name sits in the middle slot too
        ("PATRICIA RANKIN STEELE").
    Taking only the last token, as the first pass did, misses all three.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []

    def add(f: str, l: str):
        f, l = (f or "").strip(), (l or "").strip()
        if not f or not l:
            return
        k = (_norm(f), _norm(l))
        if k not in seen:
            seen.add(k)
            out.append((f, l))

    full = rec.get("decedent_name", "")
    first, last = _split_name(full)
    add(first, last)

    toks = [t for t in re.split(r"[\s,]+", full.strip()) if t and len(t) > 1]
    if len(toks) >= 3:
        add(toks[0], toks[-2])                      # middle token as surname
        add(toks[0], f"{toks[-2]} {toks[-1]}")      # compound surname

    for aka in re.findall(r"aka\s+([A-Za-z][A-Za-z.\s'\-]{3,50}?)(?=\s*(?:,|aka|CASE|$))",
                          rec.get("raw_excerpt", ""), re.I):
        af, al = _split_name(aka)
        add(af, al)
    return out


def _parse_date(s: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _addr_recency(a: dict):
    return _parse_date(a.get("lastReportedDate") or "") or datetime.min


_PO_BOX = re.compile(r"\b(?:p\.?\s*o\.?\s*box|post office box|pmb)\b", re.I)


def _is_oc_ca(a: dict) -> bool:
    """Orange County CALIFORNIA specifically.

    There is an Orange County in Florida (Orlando/Winter Park) and in several other
    states. Matching the county name alone put "Po Box 4092, Winter Park 32793" on a
    Santa Ana estate in the first wide run, which is a wrong address on a real record
    -- worse than no address at all. State is not optional here.
    """
    if _norm(a.get("county") or "") != TARGET_COUNTY:
        return False
    full = a.get("fullAddress") or ""
    st = (a.get("state") or "").strip().upper()
    if st:
        return st == TARGET_STATE
    return bool(re.search(r",\s*CA\s*\d{5}", full))


def pick_address(person: dict) -> dict | None:
    """Most recently reported Orange County, CA street address.

    addressOrder == 1 is Enformion's own "current" marker, but it is not always
    present (the Kruggel record's orders started at 2), so recency is the tie-break
    and the fallback rather than an assumption. PO boxes are rejected outright: a
    box is a mailing address, and we are trying to identify a PROPERTY.
    """
    addrs = [a for a in (person.get("addresses") or [])
             if _is_oc_ca(a) and not _PO_BOX.search(a.get("fullAddress") or "")]
    if not addrs:
        return None
    current = [a for a in addrs if str(a.get("addressOrder")) == "1"]
    pool = current or addrs
    return max(pool, key=_addr_recency)


MAX_DOD_GAP_YEARS = 3   # same convention as obituary_enricher


def _dod_of(person: dict) -> str:
    for d in (person.get("datesOfDeath") or []):
        if isinstance(d, str) and d.strip():
            return d.strip()
        if isinstance(d, dict):
            v = d.get("date") or d.get("dateOfDeath") or ""
            if v:
                return str(v)
    return str(person.get("dod") or "")


def _pr_in_relatives(person: dict, rec: dict) -> tuple[bool, str]:
    """Is the personal representative in this candidate's relatives graph?

    relativesSummary entries carry firstName/lastName at the TOP LEVEL, not nested
    under a `name` object the way the person record does. Reading it as `r["name"]`
    silently matched nothing and threw away the single best discriminator on a
    common name -- 49 relatives were sitting right there on the John Doyle record.
    """
    pr_f, pr_l = _split_name(rec.get("personal_representative", ""))
    pr_f, pr_l = _norm(pr_f), _norm(pr_l)
    if not (pr_f and pr_l):
        return False, ""
    for r in (person.get("relativesSummary") or []):
        if _norm(r.get("firstName")) == pr_f and _norm(r.get("lastName")) == pr_l:
            return True, (r.get("relativeType") or "relative")
    return False, ""


def candidate_rank(person: dict, addr: dict, rec: dict) -> tuple[int, list[str]]:
    """Rank ONE candidate. Only evidence that distinguishes this person from the
    other 49 results counts.

    The first version ranked on `score()`, which folds in record-level facts like
    "the PR shares the decedent's surname". That is true of every candidate for a
    given record, so it added a constant and left the ordering effectively
    arbitrary -- which is why one estate's address moved between two runs.

    Date of death is the strongest discriminator available: the person we want is
    dead, and died recently enough for probate to be opening now.
    """
    pts, sig = 0, []

    dod = _dod_of(person)
    if dod:
        d = _parse_date(dod)
        pub = _parse_date(rec.get("date_published", "")) or datetime.now()
        if d and 0 <= (pub - d).days / 365.25 <= MAX_DOD_GAP_YEARS:
            pts += 5
            sig.append(f"date of death {dod} consistent with a {rec.get('date_published','')} filing")
        else:
            pts += 1
            sig.append(f"date of death {dod} on record (outside the {MAX_DOD_GAP_YEARS}y window)")

    hit, rel_type = _pr_in_relatives(person, rec)
    if hit:
        pts += 5
        sig.append(f"PR is in this person's relatives graph as {rel_type}")

    # NOTE: having an Orange County address is deliberately NOT scored here.
    # This function answers "is this the right person", which must be decided
    # independently of whether that person happens to have a usable address --
    # otherwise a same-named stranger with an OC address outranks the correctly
    # identified decedent, and we publish the stranger's house.

    # middle name agreement, a cheap tiebreak between same-name candidates
    toks = [t for t in re.split(r"[\s,]+", rec.get("decedent_name", "")) if len(t) > 1]
    if len(toks) >= 3:
        mid = _norm(toks[1])
        if mid and _norm((person.get("name") or {}).get("middleName")) == mid:
            pts += 1
            sig.append("middle name matches the notice")
    return pts, sig


def score(person: dict, addr: dict, rec: dict, n_persons: int) -> tuple[str, list[str]]:
    sig: list[str] = []
    if addr:
        sig.append("address in Orange County (venue match)")

    dec_last = _norm(_split_name(rec.get("decedent_name", ""))[1])
    pr_last = _norm(_split_name(rec.get("personal_representative", ""))[1])
    if dec_last and dec_last == pr_last:
        sig.append("PR shares decedent surname")

    hit, rel_type = _pr_in_relatives(person, rec)
    if hit:
        sig.append(f"PR present in decedent's relatives graph as {rel_type}")

    if person.get("datesOfDeath") or person.get("dod"):
        sig.append("Enformion carries a date of death")

    if n_persons == 1:
        sig.append("unambiguous name (single match)")

    if not addr:
        return "unresolved", sig
    if len(sig) >= 3:
        conf = "high"
    elif len(sig) == 2:
        conf = "medium"
    else:
        conf = "low"
    return conf, sig


def resolve_one(rec: dict, verbose: bool = True, per_page: int = 5) -> dict:
    out = dict(rec)
    out.update({"resolved_address": "", "resolved_city": "", "resolved_zip": "",
                "resolve_confidence": "unresolved", "resolve_signals": "",
                "resolve_source": "", "address_last_reported": "",
                "decedent_dod": "", "candidate_count": 0, "must_verify": "",
                "resolved_via_name": "", "match_points": 0, "ambiguous": "",
                "oc_namesakes": 0})
    variants = name_variants(rec)
    if not variants:
        out["resolve_signals"] = "decedent name not parseable into first/last"
        return out

    # Score EVERY candidate across every name variant and take the best, rather
    # than the first one that happens to carry an Orange County address. With a
    # 25-deep result set on a common name, "first with an OC address" is close to
    # arbitrary -- it moved Robert Mosier's address between two runs that differed
    # only in page size. Corroboration decides, not result order.
    # Collect every candidate across every name variant, then decide once.
    pool: list[tuple[int, dict, dict | None, str]] = []   # (identity_pts, person, addr, name)
    total = 0
    for first, last in variants:
        res = eh.person_search(first, last, state=TARGET_STATE,
                               results_per_page=per_page)
        persons = res.get("persons") or []
        total = max(total, len(persons))
        for p in persons:
            a = pick_address(p)
            pts, _ = candidate_rank(p, a, rec)
            pool.append((pts, p, a, f"{first} {last}"))
        time.sleep(0.25)
        if any(pts >= 5 and a for pts, _, a, _n in pool):
            break     # positively identified AND addressable; stop paying

    if not pool:
        out["resolve_signals"] = "no Enformion match on any name variant (misses not billed)"
        if verbose:
            print(f"    {rec.get('decedent_name','')[:26]:26} -> no match "
                  f"({len(variants)} variants tried)")
        return out

    out["candidate_count"] = total
    identified = [t for t in pool if t[0] >= 5]      # real identity evidence
    with_addr = [t for t in pool if t[2]]
    oc_people = len({id(t[1]) for t in with_addr})

    if identified:
        # We know WHO. Use their address, or admit they have none on file.
        identified.sort(key=lambda t: (t[0], _addr_recency(t[2]) if t[2] else datetime.min),
                        reverse=True)
        addressed = [t for t in identified if t[2]]
        chosen = addressed[0] if addressed else identified[0]
        if not chosen[2]:
            out["resolve_confidence"] = "unresolved"
            out["resolve_signals"] = ("identified the decedent (PR/death corroborated) but that "
                                      "person has no Orange County CA street address on file")
            out["resolved_via_name"] = chosen[3]
            if verbose:
                print(f"    {rec.get('decedent_name','')[:26]:26} -> identified, "
                      f"but no OC CA address on file  [{total} cand]")
            return out
    elif with_addr:
        # No identity evidence at all: venue is the only anchor. That is a real
        # signal when ONE same-named person lives in the county and weak when five do.
        with_addr.sort(key=lambda t: _addr_recency(t[2]), reverse=True)
        chosen = with_addr[0]
    else:
        out["resolve_signals"] = "no candidate has an Orange County CA street address"
        if verbose:
            print(f"    {rec.get('decedent_name','')[:26]:26} -> no OC CA address  [{total} cand]")
        return out

    best_person, best_addr, used = chosen[1], chosen[2], chosen[3]

    out["candidate_count"] = total
    out["resolved_via_name"] = used
    if best_person is None:
        out["resolve_signals"] = "no Enformion match on any name variant (misses not billed)"
        if verbose:
            print(f"    {rec.get('decedent_name','')[:26]:26} -> no match "
                  f"({len(variants)} variants tried)")
        return out

    pts, sig = candidate_rank(best_person, best_addr, rec)
    _, rec_sig = score(best_person, best_addr, rec, total)
    for s in rec_sig:
        if s not in sig:
            sig.append(s)
    if used and _norm(used) != _norm(rec.get("decedent_name", "")):
        sig.append(f"matched on name variant '{used}'")

    # Confidence combines identity evidence with how uniquely the county anchor
    # pins the person. One same-named person in Orange County is a real match
    # (this is exactly how the Sunada record resolved); five is a coin flip.
    if oc_people == 1:
        sig.append("only one same-named person in Orange County CA")
    else:
        sig.append(f"{oc_people} same-named people have Orange County CA addresses")

    if not best_addr:
        conf = "unresolved"
    elif pts >= 5 and oc_people == 1:
        conf = "high"
    elif pts >= 5:
        conf = "medium"
    elif oc_people == 1:
        conf = "medium"
    else:
        conf = "low"
    out["match_points"] = pts
    out["oc_namesakes"] = oc_people
    out["ambiguous"] = "yes" if (oc_people > 1 and pts < 5) else "no"
    if best_addr:
        full = best_addr.get("fullAddress") or ""
        # "29552 Silverado Canyon Rd; Silverado, CA 92676"
        m = re.match(r"\s*(.+?);\s*(.+?),\s*([A-Z]{2})\s*(\d{5})", full)
        if m:
            out["resolved_address"] = m.group(1).strip()
            out["resolved_city"] = m.group(2).strip()
            out["resolved_zip"] = m.group(4)
        else:
            out["resolved_address"] = full
        out["address_last_reported"] = best_addr.get("lastReportedDate") or ""
        out["resolve_source"] = "Enformion decedent address history"

    dods = best_person.get("datesOfDeath") or []
    if dods:
        d0 = dods[0]
        out["decedent_dod"] = d0 if isinstance(d0, str) else (d0.get("date") or "")

    out["resolve_confidence"] = conf
    out["resolve_signals"] = "; ".join(sig)
    out["must_verify"] = (
        "Address is the decedent's last reported residence, not proof of title or "
        "of estate ownership. Confirm against the OC Clerk-Recorder grantor/grantee "
        "index (or the Inventory & Appraisal once filed) before marketing."
    )
    if verbose:
        loc = f"{out['resolved_address']}, {out['resolved_city']} {out['resolved_zip']}".strip(" ,")
        print(f"    {rec.get('decedent_name','')[:26]:26} -> {conf:9} "
              f"{loc or '(no OC address)'}  [{total} cand]")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve OC CA probate property addresses")
    ap.add_argument("--in", dest="inp", default="output/occa_ftm_20260901.json")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true", help="show what would be searched, spend nothing")
    ap.add_argument("--no-drive", action="store_true",
                    help="do not upload the output to Google Drive (upload is the default)")
    ap.add_argument("--per-page", type=int, default=25,
                    help="Enformion results per search. The 5 default silently hid the "
                         "right person on every common name in the first live run.")
    args = ap.parse_args()

    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    recs = data["records"][:args.limit]
    print(f"Resolving property addresses for {len(recs)} Orange County probate records")
    print(f"  method : Enformion Person Search on the DECEDENT, Orange County address history")
    print(f"  note   : CA Assessor cannot be searched by owner name, so this is the route\n")

    if args.dry_run:
        for r in recs:
            f, l = _split_name(r.get("decedent_name", ""))
            print(f"    would search: {f} {l}   (PR: {r.get('personal_representative')})")
        print(f"\ndry run, nothing spent ({len(recs)} searches at ~$0.10 per MATCH, misses free)")
        return 0

    if not eh.is_configured():
        print("FAIL: ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD not set")
        return 2

    out_recs = []
    for i, r in enumerate(recs, 1):
        print(f"  [{i}/{len(recs)}]", end=" ")
        try:
            out_recs.append(resolve_one(r, per_page=args.per_page))
        except Exception as e:
            print(f"ERROR {e}")
            bad = dict(r)
            bad["resolve_confidence"] = "error"
            bad["resolve_signals"] = str(e)[:200]
            out_recs.append(bad)
        time.sleep(0.4)

    from collections import Counter
    tally = Counter(r["resolve_confidence"] for r in out_recs)
    resolved = [r for r in out_recs if r.get("resolved_address")]
    print(f"\n{len(resolved)}/{len(out_recs)} resolved to an Orange County address")
    for k in ("high", "medium", "low", "unresolved", "error"):
        if tally.get(k):
            print(f"    {k:11} {tally[k]}")

    base = args.out or f"output/occa_probate_resolved_{datetime.now():%Y%m%d}"
    Path(base).parent.mkdir(parents=True, exist_ok=True)
    Path(base + ".json").write_text(
        json.dumps({"source": args.inp, "resolved": len(resolved),
                    "records": out_recs}, indent=2), encoding="utf-8")
    cols = [c for c in out_recs[0].keys() if c != "raw_excerpt"]
    with open(base + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in out_recs:
            w.writerow(r)
    print(f"\nwrote {base}.json and {base}.csv")

    # This is the file that goes to Drive: probate records only become useful once
    # they carry a property address. The filename is date-stamped so a batch stays
    # findable in Drive -- that is where the dated identity lives now, NOT as a
    # DataSift list (Ty, 2026-09-02).
    drive_link = ""
    if not args.no_drive:
        from drive_autoupload import upload_outputs
        res = upload_outputs([base + ".csv"],
                             subfolder_note="Orange County CA probate, addresses resolved")
        if res.get("uploaded"):
            drive_link = res["uploaded"][0][1]

    try:
        from slack_notifier import send_batch_summary
        weak = tally.get("low", 0) + tally.get("unresolved", 0)
        send_batch_summary(
            "Address resolution - Orange County CA probate",
            {"records": len(out_recs),
             "resolved to an OC address": len(resolved),
             "high confidence": tally.get("high", 0),
             "medium confidence": tally.get("medium", 0),
             "low confidence": tally.get("low", 0),
             "unresolved": tally.get("unresolved", 0)},
            warnings=([f"{tally.get('low', 0)} addresses are low confidence "
                       f"(several same-named people in the county) - filter on "
                       f"address_low_confidence before marketing"]
                      if tally.get("low") else None),
            links=[("CSV", drive_link)] if drive_link else None,
        )
    except Exception as e:                      # noqa: BLE001
        print("  notification skipped: %s" % str(e)[:140])
    return 0


if __name__ == "__main__":
    sys.exit(main())
