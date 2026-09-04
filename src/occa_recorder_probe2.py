"""Diagnostic v2: same OC RecorderWorks Document Type search, but every step is a
JS `execute` that reads back real DOM/network state (not just a native `click`/`fill`
step, whose success is invisible from outside), plus the scenario's `xhr_call` log so
we can see whether AjaxPresentor.aspx actually got hit and what it returned.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import config  # noqa: E402
from scrapfly import ScrapflyClient, ScrapeConfig  # noqa: E402

URL = "https://cr.occlerkrecorder.gov/RecorderWorksInternet/"
DOCTYPE_TAB_LINK = 'a.tabItem[href="#tabs-nohdr-4"]'

CHECK_AND_READ = """
var cb=document.getElementById('MainContent_MainMenu1_SearchByDocType1_DocumentTypes1_chType326');
if(!cb) return JSON.stringify({error:'checkbox not found'});
if(!cb.checked) cb.click();
return JSON.stringify({checked:cb.checked, docTypes:document.getElementById('MainContent_MainMenu1_SearchByDocType1_docTypes').value, docNames:document.getElementById('MainContent_MainMenu1_SearchByDocType1_docNames').value});
"""

SET_DATES = """
function setVal(id,val){
  var el=document.getElementById(id);
  var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  setter.call(el,val);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  el.dispatchEvent(new Event('blur',{bubbles:true}));
  return el.value;
}
var f=setVal('MainContent_MainMenu1_SearchByDocType1_FromDate','09/03/2025');
var t=setVal('MainContent_MainMenu1_SearchByDocType1_ToDate','09/03/2026');
return JSON.stringify({from:f,to:t});
"""

CLICK_SEARCH = """
document.getElementById('MainContent_MainMenu1_SearchByDocType1_btnSearch').click();
return 'clicked';
"""

# Bypass OnSearch/SearchHelpPresentor entirely and call search.Search() directly with
# the exact query string captured from the real click (probe2 run 1), to test whether
# AjaxPresentor.aspx alone returns data when hit directly.
DIRECT_SEARCH = """
var q = '&FromDate=09/03/2025&ToDate=09/03/2026&DocumentTypes=210,&DocumentNames=NT TRUSTEE SALE,&ERetrievalGroup=1&SearchMode=3&IsNewSearch=true';
search.Search(q, null, null);
return 'direct-search-fired';
"""

READ_RESULTS = """
var rc = document.getElementById('MainContent_ResultsContainer1_CtrlWidget');
var mb = document.getElementById('MainContent_MessageBox1_CtrlWidget');
var amb = document.getElementById('MainContent_AlertMessageBox_CtrlWidget');
return JSON.stringify({
  docTypes: document.getElementById('MainContent_MainMenu1_SearchByDocType1_docTypes').value,
  fromVal: document.getElementById('MainContent_MainMenu1_SearchByDocType1_FromDate').value,
  toVal: document.getElementById('MainContent_MainMenu1_SearchByDocType1_ToDate').value,
  resultsHTMLLen: rc ? rc.innerHTML.length : -1,
  resultsHTML: rc ? rc.innerHTML.slice(0,2000) : null,
  msgBoxClass: mb ? mb.className : null,
  msgBoxText: mb ? mb.innerText.slice(0,500) : null,
  alertBoxClass: amb ? amb.className : null,
  alertBoxText: amb ? amb.innerText.slice(0,500) : null
});
"""


def main() -> int:
    client = ScrapflyClient(key=config.SCRAPFLY_KEY)
    resp = client.scrape(ScrapeConfig(
        url=URL,
        render_js=True, asp=True, country=config.SCRAPFLY_COUNTRY,
        proxy_pool="public_residential_pool",
        rendering_wait=1000,
        js_scenario=[
            {"wait_for_selector": {"selector": DOCTYPE_TAB_LINK, "timeout": 15000}},
            {"click": {"selector": DOCTYPE_TAB_LINK}},
            {"wait": 800},
            {"execute": {"script": CHECK_AND_READ}},
            {"execute": {"script": SET_DATES}},
            {"execute": {"script": DIRECT_SEARCH}},
            {"wait": 8000},
            {"execute": {"script": READ_RESULTS}},
        ],
        raise_on_upstream_error=False,
    ))
    sr = resp.scrape_result
    bd = sr.get("browser_data") or {}
    steps = ((bd.get("js_scenario") or {}).get("steps")) or []

    print(f"upstream status: {sr.get('status_code')}  success: {sr.get('success')}")
    for i, s in enumerate(steps):
        print(f"\n--- step {i} [{s.get('action')}] success={s.get('success')} ---")
        r = s.get("result")
        print(r if r else s.get("config"))

    xhr = bd.get("xhr_call") or []
    print(f"\n\n=== xhr_call log ({len(xhr)} entries) ===")
    for x in xhr:
        print(json.dumps(x, indent=2)[:1200])
        print("---")

    Path("output/occa_recorder_probe2_raw.json").write_text(
        json.dumps(bd, indent=2, default=str), encoding="utf-8"
    )
    print("\nwrote output/occa_recorder_probe2_raw.json")

    Path("output/occa_recorder_probe2_page.html").write_text(
        sr.get("content", "") or "", encoding="utf-8"
    )
    print("wrote output/occa_recorder_probe2_page.html "
          f"({len(sr.get('content') or '')} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
