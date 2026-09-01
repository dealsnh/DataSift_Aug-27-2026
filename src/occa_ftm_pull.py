"""
Orange County CA first-to-market pull: Foreclosure + Probate.

Source: www.capublicnotice.com (the SINGULAR-domain iPublish front end).
Chosen over capublicnotices.com (plural) because its county filter genuinely
works -- every result row carries a `.location` field, so Orange County is
established by the SOURCE rather than by the body-text heuristics the plural
site forces (measured there at ~1.4% yield, 1 usable hit in 72 candidates).

Three site behaviours this module exists to handle, all verified live 2026-09-01:

1. THE SEARCH IS SESSION-GATED. A cold GET to /search/query returns zero rows
   no matter how correct the parameters are. You must first load a landing page
   in the same browser context to establish the session. This is why an earlier
   pass concluded the date filter was broken: it was reading cold-session
   emptiness as "no data".
2. THE CATEGORY TAXONOMY IS NOT TRUSTWORTHY. Category 8 "Notice to Creditors"
   returns name-change orders for Orange County. So we do NOT filter by
   category; we pull every notice for the county over a window and classify
   client-side on the notice text using the canonical phrase rules.
3. NOTICES REPUBLISH WEEKLY. The same trustee sale appears once per publication
   week under a different advert id, so dedup is on the T.S. number / case
   number / APN, not the advert id.

Detail pages (/advert/-Notices_<id>) serve fine to plain requests, no browser.

Usage:
    python src/occa_ftm_pull.py --months 3 --limit 15
    python src/occa_ftm_pull.py --months 12 --types foreclosure
    python src/occa_ftm_pull.py --months 3 --no-detail     # list scan only
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE = "https://www.capublicnotice.com"
SEARCH = BASE + "/search/query"
WARMUP = BASE + "/"
ADVERT = BASE + "/advert/-{aid}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# The `page` parameter is unreliable (page 1 returns page 0's rows verbatim, page 2
# jumps to the correct next block). `size` however is honoured well past 100 and the
# server simply returns everything it has when asked for more than exists, so we ask
# for one oversized page instead of paginating. Measured: 12 months of Orange County
# is 7,084 notices, returned in a single request.
FETCH_SIZE = 9000

# ── Foreclosure phrase rules (verbatim from the first-market-county-data skill) ──
INCLUDE_PHRASES = [
    "substitute trustee's notice of sale", "substitute trustee's sale",
    "substitute trustee's notice of foreclosure sale", "substitute trustee sale",
    "substituted trustee's sale", "substituted trustee sale",
    "notice of substitute trustee's sale", "notice of substitute trustee sale",
    "successor trustee's notice of sale", "successor trustee's sale",
    "successor trustee sale", "notice of trustee's sale",
    "notice of trustee's foreclosure sale", "notice of trustee sale",
    "trustee's sale", "trustee sale",
    "notice of default and foreclosure sale", "foreclosure sale notice",
    "notice of foreclosure sale",
]
EXCLUDE_PHRASES = [
    "non-resident notice", "non resident notice", "nonresident notice",
    "order of publication", "notice to creditors", "notice of lien",
    "order to sell", "divorce", "dissolution",
]

# CA probate: the statutory notice is "Notice of Petition to Administer Estate"
# (Prob. Code s.8100). "notice to creditors" is an EXCLUDE for foreclosure but a
# legitimate probate anchor, which is exactly why the two classifiers are separate.
PROBATE_ANCHORS = [
    "petition to administer estate", "petition for probate",
    "letters testamentary", "letters of administration",
    "independent administration of estates act",
]
PROBATE_SUPPORT = ["decedent", "personal representative", "probate code", "estate of"]

# Things that look probate-ish but are not an estate opening.
PROBATE_NOT = [
    "order to show cause for change of name", "change of name",
    "fictitious business name", "summons", "notice of public sale",
]


def is_foreclosure(text: str) -> bool:
    """Skill rule order: excludes win, then includes, then the trustee guard."""
    t = " ".join(text.lower().split())
    if any(e in t for e in EXCLUDE_PHRASES):
        return False
    for inc in INCLUDE_PHRASES:
        if inc in t:
            return True
    if "notice of sale" in t and "trustee" in t:
        return True
    return False


def is_probate(text: str) -> bool:
    t = " ".join(text.lower().split())
    if any(n in t for n in PROBATE_NOT):
        # a name-change order can still mention "decedent"; anchor beats support
        if not any(a in t for a in PROBATE_ANCHORS):
            return False
    if any(a in t for a in PROBATE_ANCHORS):
        return True
    return False


# ── record ───────────────────────────────────────────────────────────────
@dataclass
class Notice:
    notice_type: str = ""
    county: str = "Orange"
    state: str = "CA"
    address: str = ""
    city: str = ""
    zip: str = ""
    owner_name: str = ""            # foreclosure: trustor. probate: PR/executor.
    decedent_name: str = ""
    personal_representative: str = ""
    auction_date: str = ""
    case_number: str = ""
    apn: str = ""
    unpaid_balance: str = ""
    trustee: str = ""
    hearing_date: str = ""
    attorney: str = ""
    pr_mailing: str = ""
    date_published: str = ""
    source_paper: str = ""
    source_url: str = ""
    advert_id: str = ""
    dedup_key: str = ""
    disqualified: str = ""
    raw_excerpt: str = ""


# ── scraping ─────────────────────────────────────────────────────────────
def search_url(page: int, first: str, last: str, county: str = "Orange",
               categories: str = "", size: int = FETCH_SIZE) -> str:
    return SEARCH + "?" + urlencode({
        "page": page, "size": size, "view": "list", "showExtended": "false",
        "startRange": "", "keywords": "",
        "firstDate": first, "lastDate": last,
        "categories": categories, "_categories": "1",
        "county": county, "_county": "", "_city": "", "_source": "",
        "ordering": "BY_DATE_DEC",
    })


def scan_list(first: str, last: str, county: str = "Orange", verbose: bool = True) -> list[dict]:
    """Paginate the county's whole notice feed for the window. Needs a warm session."""
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    seen_ids: set[str] = set()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA)
        pg = ctx.new_page()

        # THE WARM-UP. Without this every query below returns zero rows.
        pg.goto(WARMUP, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(2500)

        pg.goto(search_url(0, first, last, county),
                wait_until="domcontentloaded", timeout=300000)
        pg.wait_for_timeout(6000)
        nodes = pg.query_selector_all(".panel.panel-result")
        if verbose:
            print(f"    single oversized request returned {len(nodes)} rows")
        for n in nodes:
            t = n.query_selector("time")
            src = n.query_selector("h4")
            desc = n.query_selector(".description")
            cat = n.query_selector(".badge.category")
            aid_el = n.query_selector("input[id^=advertId_]")
            aid = aid_el.get_attribute("value") if aid_el else ""
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            rows.append({
                "advert_id": aid,
                "date": (t.get_attribute("datetime") or "")[:10] if t else "",
                "paper": (src.inner_text() or "").strip() if src else "",
                "category": (cat.inner_text() or "").strip() if cat else "",
                "desc": (desc.inner_text() or "").strip() if desc else "",
            })
        if len(nodes) >= FETCH_SIZE and verbose:
            print(f"    WARNING: hit the {FETCH_SIZE} ceiling, window may be truncated")
        ctx.close()
        b.close()
    return rows


_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style).*?</\1>", re.S | re.I)


def fetch_detail(advert_id: str, session: requests.Session) -> str:
    url = ADVERT.format(aid=advert_id)
    r = session.get(url, timeout=45)
    r.raise_for_status()
    html = _SCRIPT.sub(" ", r.text)
    txt = _TAG.sub("\n", html)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&#39;", "'").replace("&quot;", '"')
              .replace("&rsquo;", "'").replace("&ldquo;", '"').replace("&rdquo;", '"'))
    txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
    # drop the site chrome above the notice body
    for marker in ("Published in", "OK\n"):
        i = txt.find(marker)
        if i > 0:
            txt = txt[i:]
            break
    return txt


# ── parsing ──────────────────────────────────────────────────────────────
def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip(" ,;:.")


_NAME_BAD = re.compile(
    r"\d|\b(?:street|st|ave|avenue|road|rd|drive|dr|court|ct|lane|ln|blvd|way|suite|ste|"
    r"attorney|law|firm|esq|telephone|phone|pro per|p\.?o\.? box|court|clerk)\b", re.I)


def _looks_like_name(s: str) -> bool:
    """Reject address / attorney / court fragments masquerading as a person."""
    s = _clean(s)
    if not (4 <= len(s) <= 70):
        return False
    if _NAME_BAD.search(s):
        return False
    return 1 < len(s.split()) <= 6


def parse_foreclosure(text: str, rec: Notice) -> Notice:
    flat = " ".join(text.split())

    m = re.search(r"Trustor:?\s*(.+?)(?:\s*Duly Appointed|\s*Recorded\b|\s*Trustee:)", flat, re.I)
    if m:
        rec.owner_name = _clean(m.group(1))[:160]

    m = re.search(r"Duly Appointed Trustee:?\s*(.+?)(?:\s*Recorded\b|\s*Date of Sale)", flat, re.I)
    if m:
        rec.trustee = _clean(m.group(1))[:120]

    m = re.search(r"Date of Sale:?\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", flat, re.I)
    if m:
        rec.auction_date = m.group(1)

    m = re.search(r"(?:A\.?P\.?N\.?|APN)[:\s#]*([0-9][0-9\-\s]{5,20})", flat, re.I)
    if m:
        rec.apn = _clean(m.group(1))

    m = re.search(r"(?:T\.?S\.?\s*(?:No|Number)\.?|File No\.?)[:\s]*([A-Za-z0-9\-]+)", flat, re.I)
    if m:
        rec.case_number = _clean(m.group(1))

    m = re.search(r"unpaid balance and other charges:?\s*\$?([\d,]+\.?\d{0,2})", flat, re.I)
    if m:
        rec.unpaid_balance = m.group(1)

    # Street address block: label then address then "City, California ZIP"
    m = re.search(
        r"Street Address or other common designation of real property:?\s*"
        r"(.{5,120}?)\s*,?\s*([A-Za-z .'\-]{3,40}),?\s*(?:California|CA)\s*,?\s*(\d{5})",
        text.replace("\n", " "), re.I)
    if m:
        rec.address = _clean(m.group(1))
        rec.city = _clean(m.group(2)).title()
        rec.zip = m.group(3)
    else:
        m = re.search(r"Property Address:?\s*(.{5,120}?),\s*([A-Za-z .'\-]{3,40}),?\s*"
                      r"(?:California|CA)\s*,?\s*(\d{5})", flat, re.I)
        if m:
            rec.address = _clean(m.group(1))
            rec.city = _clean(m.group(2)).title()
            rec.zip = m.group(3)

    rec.dedup_key = (rec.case_number or "") + "|" + (rec.apn or "") or rec.advert_id
    return rec


def parse_probate(text: str, rec: Notice) -> Notice:
    flat = " ".join(text.split())

    m = re.search(r"(?:ESTATE OF|estate of)[:\s]+(.+?)(?:\s*(?:aka|AKA|a\.k\.a|also known as|"
                  r"CASE|Case No|To all heirs|NOTICE|,?\s*Decedent|Deceased))", flat)
    if m:
        rec.decedent_name = _clean(m.group(1))[:120]

    # OC notices write it as "CASE# 30-2026-01591281-PR-LA-CMC" (and sometimes
    # "CASE#01591887" with no space and no prefix). Missing the "#" form is what
    # made every republication look like a distinct estate on the first live run.
    m = re.search(r"CASE\s*(?:#|NO|NUMBER)?\.?\s*[:#]?\s*"
                  r"(3\d-\d{4}-\d{6,10}(?:-[A-Z]{2,3}){0,3}|\d{2}[A-Z]{2}\d{6,10}|\d{6,12})",
                  flat, re.I)
    if m:
        rec.case_number = _clean(m.group(1)).upper()

    # The PR is whoever FILED the petition. Anchor on the statutory sentence first;
    # the loose "Petitioner:" fallback drags in the attorney block and the mailing
    # address ("in pro per: Rene Mente12365 Zig Zag Wa..." on the first live run),
    # so it is name-shaped-validated before being accepted.
    # Best anchor: the appointment sentence. It is the one place the name is
    # bounded by fixed statutory words on BOTH sides. The "filed by X in the
    # Superior Court" form is second choice because the source HTML collapses
    # line breaks without a space ("filed by Rene Mentein the Superior Court"),
    # which silently swallows the surname into the next word.
    m = re.search(r"requests?\s+(?:that\s+)?(.{3,70}?)\s+be appointed as (?:the\s+)?"
                  r"personal representative", flat, re.I)
    if m and _looks_like_name(m.group(1)):
        rec.personal_representative = _clean(m.group(1))[:120]
    if not rec.personal_representative:
        m = re.search(r"PETITION\s+(?:FOR PROBATE|TO ADMINISTER).{0,60}?\s+"
                      r"(?:has been\s+)?filed by\s+(.+?)\s+in the (?:Superior|Sup)", flat, re.I)
        if m and _looks_like_name(m.group(1)):
            rec.personal_representative = _clean(m.group(1))[:120]
    if not rec.personal_representative:
        m = re.search(r"filed by\s+(.{3,80}?)\s+(?:in the Superior Court|who requests?)", flat, re.I)
        if m and _looks_like_name(m.group(1)):
            rec.personal_representative = _clean(m.group(1))[:120]
    if not rec.personal_representative:
        m = re.search(r"(?:Petitioner|PETITIONER)\s*[:\-]\s*([A-Za-z][A-Za-z .'\-]{4,60})", flat)
        if m and _looks_like_name(m.group(1)):
            rec.personal_representative = _clean(m.group(1))[:120]

    m = re.search(r"(?:hearing.{0,40}?on|HEARING.{0,30}?on)\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}"
                  r"|\d{1,2}/\d{1,2}/\d{4})", flat)
    if m:
        rec.hearing_date = _clean(m.group(1))

    m = re.search(r"Attorney(?:s)? for (?:Petitioner|Plaintiff).{0,20}?[:\s]+(.+?)"
                  r"(?:\s*\(\d{3}\)|\s*Telephone|\s*\d{1,5}\s+[A-Z])", flat, re.I)
    if m:
        rec.attorney = _clean(m.group(1))[:120]

    rec.owner_name = rec.personal_representative

    # The same estate publishes 3x and the case number is sometimes given in the
    # long form (30-2026-01591887-PR-PW-CMC) and sometimes short (01591887), so
    # dedupe on the core sequence rather than the literal string. Fall back to the
    # decedent, never to the advert id, which is unique per publication.
    core = ""
    if rec.case_number:
        mm = re.search(r"(\d{6,10})", rec.case_number)
        core = mm.group(1).lstrip("0") if mm else rec.case_number
    rec.dedup_key = core or ("D:" + re.sub(r"[^A-Z]", "", rec.decedent_name.upper())) or rec.advert_id
    return rec


# ── disqualifiers ────────────────────────────────────────────────────────
def disqualify(rec: Notice, today: datetime) -> str:
    if rec.notice_type == "foreclosure":
        if not rec.address:
            return "no street address (vacant land or unstated)"
        if re.match(r"^\s*vacant\s+land", rec.address, re.I):
            return "vacant land, no usable address"
        if rec.auction_date:
            try:
                d = datetime.strptime(rec.auction_date, "%m/%d/%Y")
                if d.date() < today.date():
                    return f"auction date {rec.auction_date} already passed"
            except ValueError:
                pass
        else:
            return "no auction date parsed"
    if rec.notice_type == "probate":
        if not rec.decedent_name and not rec.personal_representative:
            return "neither decedent nor PR parsed"
    return ""


# ── main ─────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Orange County CA FTM pull (foreclosure + probate)")
    ap.add_argument("--months", type=int, default=3, help="lookback window in months")
    ap.add_argument("--limit", type=int, default=15, help="max qualified records to keep")
    ap.add_argument("--types", default="foreclosure,probate")
    ap.add_argument("--county", default="Orange")
    ap.add_argument("--no-detail", action="store_true", help="list scan only, no detail fetches")
    ap.add_argument("--max-detail", type=int, default=400)
    ap.add_argument("--out", default="")
    ap.add_argument("--drive", action="store_true",
                    help="upload this intermediate CSV to Google Drive (OFF by default: "
                         "probate records have no property address until "
                         "occa_address_resolve.py has run, and only the resolved file "
                         "is worth keeping in Drive)")
    args = ap.parse_args()

    want = {t.strip().lower() for t in args.types.split(",") if t.strip()}
    today = datetime.now()
    last = today.strftime("%m/%d/%Y")
    first = (today - timedelta(days=31 * args.months)).strftime("%m/%d/%Y")

    print(f"Orange County CA first-to-market pull")
    print(f"  source : {BASE}  (county filter authoritative via row .location)")
    print(f"  window : {first} .. {last}  ({args.months} months)")
    print(f"  types  : {', '.join(sorted(want))}")
    print()

    print("[1/4] scanning county notice feed (all categories; taxonomy is unreliable)")
    rows = scan_list(first, last, args.county)
    print(f"      {len(rows)} distinct notices in window")
    if not rows:
        print("\nFAIL: zero notices. Session warm-up or site shape changed.")
        return 2

    # triage on the truncated list description first, to avoid needless detail fetches
    cands = []
    for r in rows:
        d = r["desc"]
        if "foreclosure" in want and is_foreclosure(d):
            cands.append((r, "foreclosure"))
        elif "probate" in want and is_probate(d):
            cands.append((r, "probate"))
    print(f"[2/4] {len(cands)} candidates from truncated descriptions "
          f"(forecl={sum(1 for _, t in cands if t == 'foreclosure')}, "
          f"probate={sum(1 for _, t in cands if t == 'probate')})")

    if args.no_detail:
        for r, t in cands[:40]:
            print(f"   {t:11} {r['date']} {r['paper'][:22]:22} {r['desc'][:70]}")
        return 0

    print(f"[3/4] fetching full notice text for up to {args.max_detail} candidates")
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    recs: list[Notice] = []
    for i, (r, t) in enumerate(cands[:args.max_detail], 1):
        try:
            text = fetch_detail(r["advert_id"], sess)
        except Exception as e:
            print(f"      [{i}] {r['advert_id']} detail FAIL {e}")
            continue
        # re-classify on the FULL text; the truncated blurb can mislead either way
        if is_foreclosure(text):
            ntype = "foreclosure"
        elif is_probate(text):
            ntype = "probate"
        else:
            continue
        if ntype not in want:
            continue
        rec = Notice(
            notice_type=ntype,
            county=args.county,
            date_published=r["date"],
            source_paper=r["paper"],
            source_url=ADVERT.format(aid=r["advert_id"]),
            advert_id=r["advert_id"],
            raw_excerpt=" ".join(text.split())[:400],
        )
        rec = parse_foreclosure(text, rec) if ntype == "foreclosure" else parse_probate(text, rec)
        rec.disqualified = disqualify(rec, today)
        recs.append(rec)
        if i % 20 == 0:
            print(f"      ...{i} fetched")
        time.sleep(0.35)

    # dedupe: republished notices share a T.S./case number, keep the most recent
    best: dict[str, Notice] = {}
    for rec in recs:
        k = (rec.notice_type, rec.dedup_key or rec.advert_id)
        prev = best.get(k)
        if prev is None or rec.date_published > prev.date_published:
            best[k] = rec
    deduped = sorted(best.values(), key=lambda r: r.date_published, reverse=True)

    qualified = [r for r in deduped if not r.disqualified]
    dropped = [r for r in deduped if r.disqualified]

    print(f"\n[4/4] {len(recs)} parsed -> {len(deduped)} distinct -> "
          f"{len(qualified)} qualified, {len(dropped)} disqualified")
    for t in sorted(want):
        print(f"      {t:12} qualified={sum(1 for r in qualified if r.notice_type == t)}  "
              f"disqualified={sum(1 for r in dropped if r.notice_type == t)}")

    if dropped:
        print("\n  disqualified reasons:")
        from collections import Counter
        for reason, n in Counter(r.disqualified for r in dropped).most_common():
            print(f"    {n:3}  {reason}")

    keep = qualified[:args.limit]
    outdir = Path("output")
    outdir.mkdir(exist_ok=True)
    stamp = today.strftime("%Y%m%d")
    base = args.out or str(outdir / f"occa_ftm_{stamp}")

    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"window": [first, last], "county": args.county,
                   "scanned": len(rows), "candidates": len(cands),
                   "distinct": len(deduped), "qualified": len(qualified),
                   "kept": len(keep),
                   "records": [asdict(r) for r in keep],
                   "disqualified": [asdict(r) for r in dropped]}, f, indent=2)

    if keep:
        cols = [c for c in asdict(keep[0]).keys() if c != "raw_excerpt"]
        with open(base + ".csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in keep:
                w.writerow(asdict(r))

    print(f"\nwrote {base}.json" + (f" and {base}.csv" if keep else ""))

    # Drive upload is OFF here on purpose (Ty, 2026-09-02): the CSV this script
    # writes has no property address yet, and a half-finished file in the shared
    # Drive folder is worse than no file. occa_address_resolve.py uploads the
    # resolved version, which is the one anybody would actually use.
    if args.drive:
        from drive_autoupload import upload_outputs
        upload_outputs([base + ".csv"],
                       subfolder_note=f"{args.county} County {'/'.join(sorted(want))}")
    else:
        print("  (not uploading to Drive - run occa_address_resolve.py, which uploads "
              "the address-resolved CSV)")

    try:
        from slack_notifier import send_batch_summary
        send_batch_summary(
            f"FTM pull - {args.county} County CA ({'/'.join(sorted(want))})",
            {"window": f"{first} to {last}",
             "notices scanned": len(rows),
             "distinct records": len(deduped),
             "qualified": len(qualified),
             "kept": len(keep)},
            warnings=([f"{len(dropped)} disqualified "
                       f"(stale auction date or no street address)"] if dropped else None)
            + ([] if keep else ["ZERO records kept - check the source"]),
        )
    except Exception as e:                      # noqa: BLE001
        print("  notification skipped: %s" % str(e)[:140])

    print(f"\n=== KEPT {len(keep)} ===")
    for r in keep:
        who = r.owner_name or r.personal_representative or "?"
        loc = f"{r.address}, {r.city} {r.zip}".strip(" ,") or "(no property address)"
        extra = f" sale {r.auction_date}" if r.auction_date else ""
        print(f"  {r.notice_type:11} {r.date_published}  {who[:38]:38}  {loc[:46]:46}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
