"""
Orange County CA obituary pull -- the "Obituary" lead source.

Renamed from "Pre-Probate" 2026-09-11: no source exists for a CA death BEFORE
a probate case is filed (see CLAUDE.md, "Pre-probate ... still an open gap"),
but a real, current, dated obituary feed does. An obituary is itself the
trigger -- a family may want to sell before or during probate -- so this
pull targets obituaries directly rather than waiting for a court filing.

SOURCES ARE PLUGGABLE ON PURPOSE. Each entry in SOURCES is (key, label,
fetch_fn). fetch_fn(frm: date, to: date) -> list[dict] of RAW rows with keys:
    decedent_name, city, state, obit_date ("MM/DD/YYYY"), source_url,
    source_name, raw_text
Add a new source by writing one fetch_fn and appending it to SOURCES --
nothing else in this file needs to change. `--sources key1,key2` restricts
a run to specific sources (handy while testing a newly-added one alone).

Tested live 2026-09-11 against 12 candidate URLs (full per-URL results in
CLAUDE.md, "Orange County CA obituary source map"). Two sources work today:

  - OC REGISTER (ocregister.com) -- the county's own paper. WordPress REST
    API (`/wp-json/wp/v2/obituary`), clean `after`/`before` date filtering,
    plain HTTP, no CAPTCHA/Cloudflare. BUT it is actually the whole SCNG
    regional network's obituary feed, not OC-only -- live records included
    Idaho Falls ID, Sarasota FL, Bend OR, Raleigh NC in the same window.
    The decedent's city/state is not a clean API field; it is recovered from
    the URL SLUG, which SCNG's own obituary system appends as
    "<name>-<city>-<st>" (e.g. "mark-w-erickson-orange-ca"), with a prose
    fallback ("... of Anaheim, California ...") for the few records where
    the slug heuristic doesn't cleanly strip.
  - HILGENFELD MORTUARY -- a real, independent Orange County funeral home.
    Also WordPress, also has a working REST API (`/wp-json/wp/v2/obituaries`,
    note the type is plural here), same `after`/`before` filtering. Its
    list endpoint returns blank `content`, so each candidate's own page is
    fetched for the body text. It is a single OC-area mortuary, so every
    record from it is treated as Orange-County-plausible rather than
    confirmed -- flagged via `city_confirmed=False` in the output, same
    "flag don't assert" discipline the rest of this project already uses
    for low-confidence signals.

DIGNITY MEMORIAL WAS TESTED AND IS NOT WIRED IN YET. Its `?groupcode=`
URL param does NOT actually filter -- confirmed live via Scrapfly (needed;
a plain fetch 403s even on the homepage): a `groupcode=santa-ana-ca` search
returned Nashville TN, Winder GA, Apopka FL entries in the same result set,
same class of leaky filter capublicnotices.com's county dropdown had. The
real, filtered search is a client-side call this build did not reverse-
engineer (out of scope for today, see CLAUDE.md). The actual initial-load
data lives in the page's `__NEXT_DATA__.props.pageProps.obitsInit` JSON
blob if someone resumes this -- `filters.creationDate` on that same blob
confirms the site supports "last7days" server-side once the real API call
is found, which would give this source the best date control of the three.
Left as a commented-out registry entry below, not deleted, so the next
session has the entry point instead of starting from zero again.

Usage:
    python src/occa_obituary_pull.py --from-date 09/05/2026 --to-date 09/09/2026
    python src/occa_obituary_pull.py --days-back 5
    python src/occa_obituary_pull.py --sources ocregister --limit 30
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Every incorporated Orange County CA city, plus notable unincorporated
# communities. Matched case-insensitively against a source's own city field
# (or, for Hilgenfeld, accepted on the strength of it being a single OC
# mortuary). "orange county" itself is also a hit -- some obituaries just
# say the county, no city.
OC_CITIES = {
    "aliso viejo", "anaheim", "brea", "buena park", "costa mesa", "cypress",
    "dana point", "fountain valley", "fullerton", "garden grove",
    "huntington beach", "irvine", "la habra", "la palma", "laguna beach",
    "laguna hills", "laguna niguel", "laguna woods", "lake forest",
    "los alamitos", "mission viejo", "newport beach", "newport coast",
    "orange", "placentia", "rancho santa margarita", "san clemente",
    "san juan capistrano", "santa ana", "seal beach", "stanton", "tustin",
    "villa park", "westminster", "yorba linda",
    # unincorporated / notable communities
    "ladera ranch", "coto de caza", "silverado", "trabuco canyon",
    "modjeska canyon", "rossmoor", "midway city", "north tustin",
    "el modena", "sunset beach", "cowan heights", "robinson ranch",
    "orange county",
}

STATE_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy",
}


def _is_oc_city(city: str) -> bool:
    return (city or "").strip().lower() in OC_CITIES


# ── record ───────────────────────────────────────────────────────────────
@dataclass
class ObitRecord:
    notice_type: str = "obituary"
    county: str = "Orange"
    state: str = "CA"
    decedent_name: str = ""
    personal_representative: str = ""   # obituaries rarely name one; left for
                                         # occa_address_resolve.py compatibility
    city: str = ""
    city_confirmed: bool = True
    date_published: str = ""            # MM/DD/YYYY
    source_name: str = ""
    source_url: str = ""
    source_id: str = ""
    dedup_key: str = ""
    disqualified: str = ""
    raw_excerpt: str = ""


def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip(" ,;:.")


_TAG = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    txt = _TAG.sub(" ", html or "")
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&#39;", "'").replace("&#8217;", "'")
              .replace("&#8220;", '"').replace("&#8221;", '"')
              .replace("&quot;", '"').replace("&rsquo;", "'")
              .replace("&ldquo;", '"').replace("&rdquo;", '"'))
    return _clean(txt)


def _slugify(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s


def _slug_city_state(slug: str, name: str) -> tuple[str, str]:
    """Recover city/state from an SCNG-style slug: '<slugified-name>-<city>-<st>'.

    Confirmed live 2026-09-11: 'mark-w-erickson-orange-ca' -> ('orange', 'ca'),
    'david-paul-saltzer-tustin-ca' -> ('tustin', 'ca'). Strips the slugified
    NAME as a prefix (best-effort -- nicknames/suffixes can drift it), then
    treats the trailing token as state (validated against STATE_ABBR) and
    whatever remains as the city.
    """
    parts = slug.split("-")
    if not parts or parts[-1] not in STATE_ABBR:
        return "", ""
    st = parts[-1]
    name_slug = _slugify(name)
    name_parts = name_slug.split("-")
    body = parts[:-1]
    if body[:len(name_parts)] == name_parts:
        city_parts = body[len(name_parts):]
    else:
        # name didn't cleanly match the slug prefix (nickname/suffix drift);
        # fall back to the last 1-2 tokens before the state as a best guess
        city_parts = body[-2:] if len(body) >= 2 else body
    if not city_parts:
        return "", st
    return " ".join(p.capitalize() for p in city_parts), st


_PROSE_CITY_RE = re.compile(
    r"\bof\s+([A-Za-z][A-Za-z .'\-]{2,35}?),\s*"
    r"(California|CA|[A-Z]{2})\b", re.I)
_OC_PHRASE_RE = re.compile(r"\borange\s+county\b", re.I)


def _text_city(text: str) -> tuple[str, str]:
    m = _PROSE_CITY_RE.search(text)
    if m:
        st = m.group(2).upper()
        st = "CA" if st == "CALIFORNIA" else st
        return _clean(m.group(1)), st
    if _OC_PHRASE_RE.search(text):
        return "Orange County", "CA"
    return "", ""


# ── source: OC Register ─────────────────────────────────────────────────
def fetch_ocregister(frm: datetime, to: datetime, verbose: bool = True) -> list[dict]:
    base = "https://www.ocregister.com/wp-json/wp/v2/obituary"
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    rows: list[dict] = []
    page = 1
    while True:
        r = sess.get(base, params={
            "per_page": 100, "page": page,
            "after": frm.strftime("%Y-%m-%dT00:00:00"),
            "before": to.strftime("%Y-%m-%dT23:59:59"),
            "orderby": "date", "order": "desc",
        }, timeout=30)
        if r.status_code == 400:      # WP returns 400 once page exceeds total
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        total_pages = int(r.headers.get("X-WP-TotalPages", "1") or "1")
        for d in batch:
            name = _strip_html(d.get("title", {}).get("rendered", ""))
            text = _strip_html(d.get("content", {}).get("rendered", ""))
            slug = d.get("slug", "")
            city, st = _slug_city_state(slug, name)
            if not city:
                city, st = _text_city(text)
            pub = d.get("date", "")[:10]
            try:
                pub_fmt = datetime.strptime(pub, "%Y-%m-%d").strftime("%m/%d/%Y")
            except ValueError:
                pub_fmt = ""
            rows.append({
                "decedent_name": name,
                "city": city,
                "state": st,
                "obit_date": pub_fmt,
                "source_url": d.get("link", ""),
                "source_name": "OC Register",
                "source_id": str(d.get("id", "")),
                "raw_text": text,
            })
        if verbose:
            print(f"    OC Register page {page}/{total_pages}: {len(batch)} rows")
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.3)
    return rows


# ── source: Hilgenfeld Mortuary ─────────────────────────────────────────
def fetch_hilgenfeld(frm: datetime, to: datetime, verbose: bool = True) -> list[dict]:
    base = "https://www.hilgenfeldmortuary.com/wp-json/wp/v2/obituaries"
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    rows: list[dict] = []
    page = 1
    stubs: list[dict] = []
    while True:
        r = sess.get(base, params={
            "per_page": 100, "page": page,
            "after": frm.strftime("%Y-%m-%dT00:00:00"),
            "before": to.strftime("%Y-%m-%dT23:59:59"),
            "orderby": "date", "order": "desc",
        }, timeout=30)
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        total_pages = int(r.headers.get("X-WP-TotalPages", "1") or "1")
        stubs.extend(batch)
        if verbose:
            print(f"    Hilgenfeld page {page}/{total_pages}: {len(batch)} rows")
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.3)

    # list endpoint's content is blank -- fetch each candidate's own page for
    # body text. Volume for a single week off one small mortuary is expected
    # to be small, so this is cheap.
    for d in stubs:
        name = _strip_html(d.get("title", {}).get("rendered", ""))
        link = d.get("link", "")
        text = ""
        try:
            pr = sess.get(link, timeout=30)
            pr.raise_for_status()
            text = _strip_html(pr.text)
        except Exception as e:                      # noqa: BLE001
            if verbose:
                print(f"      detail fetch failed for {link}: {e}")
        city, st = _text_city(text)
        pub = d.get("date", "")[:10]
        try:
            pub_fmt = datetime.strptime(pub, "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            pub_fmt = ""
        rows.append({
            "decedent_name": name,
            "city": city,
            "state": st,
            "obit_date": pub_fmt,
            "source_url": link,
            "source_name": "Hilgenfeld Mortuary",
            "source_id": str(d.get("id", "")),
            "raw_text": text,
            "_city_unconfirmed_ok": True,   # single OC mortuary: presume OC
        })
        time.sleep(0.2)
    return rows


# ── source registry ──────────────────────────────────────────────────────
SOURCES: list[tuple[str, str, "callable"]] = [
    ("ocregister", "OC Register", fetch_ocregister),
    ("hilgenfeld", "Hilgenfeld Mortuary", fetch_hilgenfeld),
    # ("dignitymemorial", "Dignity Memorial", fetch_dignitymemorial),  # NOT
    # wired in yet -- see module docstring. Needs the real search API
    # (obitsInit's __NEXT_DATA__ blob shows the shape; groupcode does not
    # filter). Add a fetch_dignitymemorial(frm, to) with the same return
    # shape and un-comment this line once it's built.
]


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def main() -> int:
    ap = argparse.ArgumentParser(description="Orange County CA obituary pull")
    ap.add_argument("--from-date", dest="from_date", default="",
                    help="MM/DD/YYYY")
    ap.add_argument("--to-date", dest="to_date", default="",
                    help="MM/DD/YYYY, defaults to today")
    ap.add_argument("--days-back", type=int, default=0,
                    help="alternative to --from-date: N days back from --to-date")
    ap.add_argument("--sources", default="",
                    help="comma-separated source keys to restrict to (default: all wired-in)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default="")
    ap.add_argument("--drive", action="store_true",
                    help="upload this intermediate CSV to Drive (off by default -- "
                         "no property address yet, occa_address_resolve.py uploads "
                         "the resolved version)")
    args = ap.parse_args()

    today = datetime.now()
    to = datetime.strptime(args.to_date, "%m/%d/%Y") if args.to_date else today
    if args.from_date:
        frm = datetime.strptime(args.from_date, "%m/%d/%Y")
    elif args.days_back:
        frm = to - timedelta(days=args.days_back)
    else:
        frm = to - timedelta(days=7)

    want = {s.strip() for s in args.sources.split(",") if s.strip()} or None
    active = [(k, label, fn) for k, label, fn in SOURCES if want is None or k in want]
    if not active:
        print(f"FAIL: no matching sources for --sources {args.sources!r}")
        return 2

    print("Orange County CA obituary pull")
    print(f"  window  : {frm:%m/%d/%Y} .. {to:%m/%d/%Y}")
    print(f"  sources : {', '.join(label for _, label, _ in active)}")
    print()

    raw_rows: list[dict] = []
    for key, label, fn in active:
        print(f"[{label}] fetching...")
        try:
            rows = fn(frm, to)
        except Exception as e:                      # noqa: BLE001
            print(f"  FAILED: {e}")
            continue
        print(f"  {len(rows)} raw rows")
        raw_rows.extend(rows)

    print(f"\n{len(raw_rows)} total raw rows across {len(active)} source(s)")

    # OC scoping: keep only rows whose city is a real Orange County place, or
    # a source (Hilgenfeld) whose entire feed is presumptively OC.
    kept: list[ObitRecord] = []
    dropped_not_oc = 0
    for row in raw_rows:
        oc_ok = _is_oc_city(row.get("city", "")) or row.get("_city_unconfirmed_ok")
        if not oc_ok:
            dropped_not_oc += 1
            continue
        rec = ObitRecord(
            decedent_name=_clean(row.get("decedent_name", ""))[:120],
            city=_clean(row.get("city", "")),
            city_confirmed=not row.get("_city_unconfirmed_ok", False),
            date_published=row.get("obit_date", ""),
            source_name=row.get("source_name", ""),
            source_url=row.get("source_url", ""),
            source_id=row.get("source_id", ""),
            raw_excerpt=(row.get("raw_text", "") or "")[:400],
        )
        rec.dedup_key = _norm_name(rec.decedent_name) or rec.source_id
        if not rec.decedent_name:
            rec.disqualified = "no decedent name parsed"
        kept.append(rec)

    print(f"  {dropped_not_oc} dropped: not a confirmed Orange County location")

    # dedupe: same decedent from more than one source (or republished within
    # one source) -- keep the richer record (has a confirmed city, else
    # longer excerpt).
    best: dict[str, ObitRecord] = {}
    for rec in kept:
        prev = best.get(rec.dedup_key)
        if prev is None:
            best[rec.dedup_key] = rec
            continue
        prev_score = (1 if prev.city_confirmed else 0, len(prev.raw_excerpt))
        this_score = (1 if rec.city_confirmed else 0, len(rec.raw_excerpt))
        if this_score > prev_score:
            best[rec.dedup_key] = rec
    deduped = sorted(best.values(), key=lambda r: r.date_published, reverse=True)

    qualified = [r for r in deduped if not r.disqualified]
    dropped_bad = [r for r in deduped if r.disqualified]
    keep = qualified[:args.limit]

    print(f"\n{len(kept)} OC-scoped -> {len(deduped)} distinct -> "
          f"{len(qualified)} qualified -> keeping {len(keep)}")
    if dropped_bad:
        print(f"  {len(dropped_bad)} disqualified (no name parsed)")

    outdir = Path("output")
    outdir.mkdir(exist_ok=True)
    stamp = today.strftime("%Y%m%d")
    out_base = args.out or str(outdir / f"occa_obituary_{stamp}")

    with open(out_base + ".json", "w", encoding="utf-8") as f:
        json.dump({
            "window": [frm.strftime("%m/%d/%Y"), to.strftime("%m/%d/%Y")],
            "county": "Orange", "state": "CA",
            "sources": [label for _, label, _ in active],
            "raw_rows": len(raw_rows),
            "oc_scoped": len(kept),
            "distinct": len(deduped),
            "qualified": len(qualified),
            "kept": len(keep),
            "records": [asdict(r) for r in keep],
            "disqualified": [asdict(r) for r in dropped_bad],
        }, f, indent=2)

    if keep:
        cols = [c for c in asdict(keep[0]).keys() if c != "raw_excerpt"]
        with open(out_base + ".csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in keep:
                w.writerow(asdict(r))

    print(f"\nwrote {out_base}.json" + (f" and {out_base}.csv" if keep else ""))

    if args.drive:
        from drive_autoupload import upload_outputs
        upload_outputs([out_base + ".csv"], subfolder_note="Orange County CA obituaries")
    else:
        print("  (not uploading to Drive -- run occa_address_resolve.py against this "
              "file, which uploads the address-resolved CSV)")

    try:
        from slack_notifier import send_batch_summary
        send_batch_summary(
            "FTM pull - Orange County CA obituaries",
            {"window": f"{frm:%m/%d/%Y} to {to:%m/%d/%Y}",
             "sources": ", ".join(label for _, label, _ in active),
             "raw rows": len(raw_rows),
             "confirmed Orange County": len(kept),
             "distinct people": len(deduped),
             "kept": len(keep)},
            warnings=([f"{dropped_not_oc} rows dropped, not a confirmed OC location"]
                      if dropped_not_oc else [])
            + ([] if keep else ["ZERO records kept - check the sources"]),
        )
    except Exception as e:                          # noqa: BLE001
        print("  notification skipped: %s" % str(e)[:140])

    print(f"\n=== KEPT {len(keep)} ===")
    for r in keep:
        loc = f"{r.city}" + ("" if r.city_confirmed else " (unconfirmed, single-mortuary source)")
        print(f"  {r.date_published}  {r.decedent_name[:32]:32}  {loc[:45]:45}  {r.source_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
