"""
Orange County CA foreclosure pull: OC Clerk-Recorder RecorderWorks, Document Type
search, code 210 "NT TRUSTEE SALE" -- the source found and reverse-engineered
2026-09-03 (see CLAUDE.md "Orange County CA source map" for the full writeup of
why the site's own Search button silently fails in a scripted browser, and the
exact fix used below).

KNOWN CAP: the site truncates any single date-range query at 367 results
(confirmed live: a 6-month and a 12-month query both returned exactly 367, while
a 1-month query returned its true, uncapped count of 80). Default sort is
newest-first, so a capped query silently drops the OLDER end of the range, not
the newer end -- meaning a wide date range under-reports rather than erroring.
This pull takes `--from`/`--to` as a single window and does NOT yet partition by
month; keep any one call comfortably under 367 real results (recent pace is
roughly 80/month) until the month-by-month partitioner is built.

Each result row gives: Document Number, Recording Date, and a Grantor block that
mixes the trustee company with what looks like the trustor/property-owner name,
with no field distinguishing which is which. NOT YET RESOLVED: whether a
property address/APN is obtainable (from the per-record detail view, keyed off
`docid`, or only from the imaged document itself). This pull stops at the raw
Grantor/date/doc-number level; address resolution, single/multi-family
filtering, and DataSift upload are follow-on steps once that's confirmed.

Usage:
    python src/occa_recorder_pull.py --limit 60
    python src/occa_recorder_pull.py --from 08/01/2026 --to 09/03/2026 --limit 60
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import config  # noqa: E402
from scrapfly import ScrapflyClient, ScrapeConfig  # noqa: E402

URL = "https://cr.occlerkrecorder.gov/RecorderWorksInternet/"
DOCTYPE_TAB_LINK = 'a.tabItem[href="#tabs-nohdr-4"]'
CHECKBOX_210 = "#MainContent_MainMenu1_SearchByDocType1_DocumentTypes1_chType326"
PAGE_SIZE = 20

CHECK_AND_READ = f"""
var cb=document.getElementById('{CHECKBOX_210[1:]}');
if(!cb) return JSON.stringify({{error:'checkbox not found'}});
if(!cb.checked) cb.click();
return JSON.stringify({{checked:cb.checked}});
"""

SET_DATES_TMPL = """
function setVal(id,val){{
  var el=document.getElementById(id);
  var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  setter.call(el,val);
  el.dispatchEvent(new Event('input',{{bubbles:true}}));
  el.dispatchEvent(new Event('change',{{bubbles:true}}));
  return el.value;
}}
var f=setVal('MainContent_MainMenu1_SearchByDocType1_FromDate','{frm}');
var t=setVal('MainContent_MainMenu1_SearchByDocType1_ToDate','{to}');
return JSON.stringify({{from:f,to:t}});
"""

DIRECT_SEARCH_TMPL = """
var q = '&FromDate={frm}&ToDate={to}&DocumentTypes=210,&DocumentNames=NT TRUSTEE SALE,&ERetrievalGroup=1&SearchMode=3&IsNewSearch=true';
search.Search(q, null, null);
return 'direct-search-fired';
"""

GOTO_PAGE_TMPL = """
search.OnPage('{page}', '.booking');
return 'page-{page}-requested';
"""

READ_ROWS = """
var rows = document.querySelectorAll('.searchResultRow');
var out = [];
rows.forEach(function(r){
  var doc = r.querySelector('[id$="_docNumber"]');
  var date = r.querySelector('#recDate');
  var grtP = r.querySelectorAll('.GrtContainer p');
  var names = [];
  grtP.forEach(function(p){ names.push(p.innerText.trim()); });
  out.push({doc: doc ? doc.innerText.trim() : null,
            date: date ? date.innerText.trim() : null,
            grantors: names});
});
var title = document.getElementById('SearchResultsTitle1_resultCount');
return JSON.stringify({resultCount: title ? title.innerText : null, rows: out});
"""


def scrape(scenario):
    client = ScrapflyClient(key=config.SCRAPFLY_KEY)
    resp = client.scrape(ScrapeConfig(
        url=URL, render_js=True, asp=True, country=config.SCRAPFLY_COUNTRY,
        proxy_pool="public_residential_pool", rendering_wait=1000,
        js_scenario=scenario, raise_on_upstream_error=False,
    ))
    return resp.scrape_result


def pull(frm: str, to: str, limit: int, pages: list[int] | None = None) -> list[dict]:
    """pages: specific page numbers to read (1-indexed, PAGE_SIZE rows each). If
    None, reads pages 1..ceil(limit/PAGE_SIZE). Page 1 always runs (it's what
    executes the actual search); OnPage() can jump straight to any later page
    without visiting the ones in between, so a resume only pays for the pages
    it actually needs."""
    if pages is None:
        n_pages = (limit + PAGE_SIZE - 1) // PAGE_SIZE
        pages = list(range(1, n_pages + 1))
    pages = sorted(set(pages) | {1})  # page 1's search always has to run

    scenario = [
        {"wait_for_selector": {"selector": DOCTYPE_TAB_LINK, "timeout": 15000}},
        {"click": {"selector": DOCTYPE_TAB_LINK}},
        {"wait": 800},
        {"execute": {"script": CHECK_AND_READ}},
        {"execute": {"script": SET_DATES_TMPL.format(frm=frm, to=to)}},
        {"execute": {"script": DIRECT_SEARCH_TMPL.format(frm=frm, to=to)}},
        {"wait": 7000},
    ]
    if 1 in pages:
        scenario.append({"execute": {"script": READ_ROWS}})
    for p in [x for x in pages if x != 1]:
        scenario.append({"execute": {"script": GOTO_PAGE_TMPL.format(page=p)}})
        scenario.append({"wait": 5000})
        scenario.append({"execute": {"script": READ_ROWS}})

    print(f"Pulling NT TRUSTEE SALE {frm} - {to}, page(s) {pages} ...")
    sr = scrape(scenario)
    if not sr.get("success"):
        print("FAILED:", sr.get("error"))
        return []

    bd = sr.get("browser_data") or {}
    steps = ((bd.get("js_scenario") or {}).get("steps")) or []
    read_results = [s for s in steps if s.get("action") == "execute"
                     and s.get("result") and '"rows"' in (s.get("result") or "")]

    all_rows: list[dict] = []
    seen_docs = set()
    total_reported = None
    for s in read_results:
        data = json.loads(s["result"])
        total_reported = data.get("resultCount") or total_reported
        for row in data.get("rows", []):
            doc = row.get("doc")
            if not doc or doc in seen_docs:
                continue
            seen_docs.add(doc)
            all_rows.append(row)

    print(f"site reports {total_reported} total in window; pulled {len(all_rows)} "
          f"unique rows across {len(read_results)} page read(s)")
    return all_rows[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="08/01/2026")
    ap.add_argument("--to", dest="to", default="09/03/2026")
    ap.add_argument("--limit", type=int, default=60,
                     help="cap when --pages is not given (reads pages 1..ceil(limit/20))")
    ap.add_argument("--pages", default="",
                     help="explicit comma-separated page numbers to read, e.g. '4,5' "
                          "to resume right after an earlier pull of pages 1-3")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    out = args.out or f"output/occa_foreclosure_pull_{datetime.now():%Y%m%d}.json"
    existing: list[dict] = []
    if args.pages and Path(out).exists():
        existing = json.loads(Path(out).read_text(encoding="utf-8")).get("records", [])
        print(f"resuming: {len(existing)} record(s) already in {out}")

    pages = [int(p) for p in args.pages.split(",") if p.strip()] if args.pages else None
    rows = pull(args.frm, args.to, args.limit, pages=pages)
    if not rows and not existing:
        print("No rows pulled.")
        return 1

    seen = {r["doc"] for r in existing}
    merged = existing + [r for r in rows if r["doc"] not in seen]

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({
        "source": "cr.occlerkrecorder.gov/RecorderWorksInternet (Document Type 210, NT TRUSTEE SALE)",
        "window": {"from": args.frm, "to": args.to},
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(merged),
        "records": merged,
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {len(merged)} total record(s) -> {out} "
          f"({len(rows)} new this run)")

    print("\nnewest new rows this run:")
    for r in rows[:5]:
        print(f"  {r['doc']}  {r['date']}  {' | '.join(r['grantors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
