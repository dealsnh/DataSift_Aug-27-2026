"""
One-off probe: drive the OC Clerk-Recorder RecorderWorks Document Type search
(cr.occlerkrecorder.gov/RecorderWorksInternet) for code 210 "NT TRUSTEE SALE"
via a real Scrapfly browser session, and see if results actually render.

Reverse-engineered from the site's own JS (RecorderWorksClient.js + inline page
script + validator.js), pulled live 2026-09-03:
  - search.OnSearch('.searchByDocType', ...) first runs
    validation.OnSearchValidation('.searchByDocType'), which BLOCKS the search
    (silently, no POST fires) unless: at least one Document Type checkbox is
    checked (hidden field getparam=DocumentTypes must be non-empty), AND
    FromDate/ToDate are both filled (both inputs carry required="required").
  - Checking a type checkbox fires documentTypes.OnSetSelection('trN'), which
    is what actually populates the hidden DocumentTypes/DocumentNames fields --
    so the checkbox must be REAL-clicked, not just marked checked in the DOM.
  - The Document Type tab panel (#tabs-nohdr-4) is a hidden jQuery-UI tab until
    its tab link is clicked, so elements inside it are not interactable before
    that.
  - Code 210 = "NT TRUSTEE SALE", checkbox id
    MainContent_MainMenu1_SearchByDocType1_DocumentTypes1_chType326 (this id is
    NOT guaranteed stable across a redeploy of the doc-type list; re-grep the
    page's `value="210"` row if this probe starts silently checking nothing).

The working theory this probe tests: the earlier failed attempts never filled
FromDate/ToDate, so OnSearchValidation bailed before any AJAX call fired --
which look identical to "empty results container", but is actually
"no search was ever sent."
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from scrapfly_browser import ScrapflyBrowserClient, visible_text  # noqa: E402

URL = "https://cr.occlerkrecorder.gov/RecorderWorksInternet/"
DOCTYPE_TAB_LINK = 'a.tabItem[href="#tabs-nohdr-4"]'
CHECKBOX_210 = "#MainContent_MainMenu1_SearchByDocType1_DocumentTypes1_chType326"
FROM_DATE = "#MainContent_MainMenu1_SearchByDocType1_FromDate"
TO_DATE = "#MainContent_MainMenu1_SearchByDocType1_ToDate"
SEARCH_BTN = "#MainContent_MainMenu1_SearchByDocType1_btnSearch"
RESULTS = "#MainContent_ResultsContainer1_CtrlWidget"


def main() -> int:
    today = datetime.now()
    frm = (today - timedelta(days=365)).strftime("%m/%d/%Y")
    to = today.strftime("%m/%d/%Y")

    scenario = [
        {"wait_for_selector": {"selector": DOCTYPE_TAB_LINK, "timeout": 15000}},
        {"click": {"selector": DOCTYPE_TAB_LINK}},
        {"wait": 1000},
        {"wait_for_selector": {"selector": CHECKBOX_210, "timeout": 15000}},
        {"click": {"selector": CHECKBOX_210}},
        {"wait": 500},
        {"fill": {"selector": FROM_DATE, "value": frm}},
        {"wait": 300},
        {"fill": {"selector": TO_DATE, "value": to}},
        {"wait": 300},
        {"click": {"selector": SEARCH_BTN}},
        {"wait": 8000},
    ]

    print(f"Searching NT TRUSTEE SALE (code 210), {frm} - {to} ...")
    client = ScrapflyBrowserClient()
    res = client.fetch(URL, js_scenario=scenario, rendering_wait=1000, retries=0)

    if not res.ok:
        print(f"FAILED: blocked={res.blocked_reason!r} error={res.error!r} "
              f"upstream={res.upstream_status}")
        return 1

    out_html = Path("output/occa_recorder_probe.html")
    out_html.write_text(res.content, encoding="utf-8")
    print(f"OK upstream={res.upstream_status} cost={res.cost} bytes={len(res.content)}")
    print(f"wrote {out_html}")

    text = visible_text(res.content)
    # crude signal check: did the results container end up with anything in it?
    i = res.content.find(RESULTS.lstrip("#"))
    snippet = res.content[i:i + 1500] if i != -1 else "(results container id not found in HTML)"
    print("\n--- results container region (first 1500 chars) ---")
    print(snippet)

    if "please select at least one" in text.lower() or "start date" in text.lower() and "please enter" in text.lower():
        print("\n>>> VALIDATION ERROR still showing in rendered text -- scenario did not satisfy the form.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
