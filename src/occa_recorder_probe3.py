"""Diagnostic v3: after the working direct search (probe2), jump to the LAST page of
results via search.OnPage('19','.booking') to find the oldest record actually
returned within the searched date window.
"""
from __future__ import annotations

import json
import re
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
return JSON.stringify({checked:cb.checked});
"""

SET_DATES = """
function setVal(id,val){
  var el=document.getElementById(id);
  var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  setter.call(el,val);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  return el.value;
}
var f=setVal('MainContent_MainMenu1_SearchByDocType1_FromDate','08/01/2026');
var t=setVal('MainContent_MainMenu1_SearchByDocType1_ToDate','09/03/2026');
return JSON.stringify({from:f,to:t});
"""

DIRECT_SEARCH = """
var q = '&FromDate=08/01/2026&ToDate=09/03/2026&DocumentTypes=210,&DocumentNames=NT TRUSTEE SALE,&ERetrievalGroup=1&SearchMode=3&IsNewSearch=true';
search.Search(q, null, null);
return 'direct-search-fired';
"""

FIND_LAST_PAGE_NUM = """
var cells = document.querySelectorAll('.pagingCell.pagingCellNumber, .pagingCell.boldLinkColor');
var last = null;
cells.forEach(function(c){
  var m = (c.getAttribute('onclick')||'').match(/OnPage\\('(\\d+)'/);
  if (m) { var n = parseInt(m[1],10); if (last===null || n>last) last=n; }
});
window.__lastPage = last || 1;
var titleEl = document.getElementById('SearchResultsTitle1_resultCount');
var rc = document.getElementById('MainContent_ResultsContainer1_CtrlWidget');
return JSON.stringify({
  lastPage: last,
  resultCount: titleEl ? titleEl.innerText : null,
  resultsContainerLen: rc ? rc.innerHTML.length : -1,
  resultsContainerPreview: rc ? rc.innerHTML.slice(0,600) : null,
  docTypesVal: (document.getElementById('MainContent_MainMenu1_SearchByDocType1_docTypes')||{}).value
});
"""

GOTO_LAST_PAGE = """
search.OnPage(String(window.__lastPage), '.booking');
return 'page-' + window.__lastPage + '-requested';
"""

READ_LAST_PAGE = """
var rows = document.querySelectorAll('.searchResultRow');
var out = [];
rows.forEach(function(r){
  var doc = r.querySelector('[id$=\\'_docNumber\\']');
  var date = r.querySelector('#recDate');
  var grt = r.querySelector('.GrtContainer');
  out.push({doc: doc ? doc.innerText : null, date: date ? date.innerText : null,
            grantor: grt ? grt.innerText.replace(/\\n/g,' | ') : null});
});
var title = document.getElementById('SearchResultsTitle1_resultCount');
return JSON.stringify({resultCount: title ? title.innerText : null, rowCount: rows.length, rows: out});
"""


def run_step(script):
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
            {"wait": 7000},
            {"execute": {"script": FIND_LAST_PAGE_NUM}},
            {"execute": {"script": GOTO_LAST_PAGE}},
            {"wait": 6000},
            {"execute": {"script": READ_LAST_PAGE}},
        ],
        raise_on_upstream_error=False,
    ))
    return resp.scrape_result


def main() -> int:
    sr = run_step(None)
    print("status_code:", sr.get("status_code"), "success:", sr.get("success"),
          "error:", sr.get("error"))
    bd = sr.get("browser_data") or {}
    steps = ((bd.get("js_scenario") or {}).get("steps")) or []
    print(f"total steps executed: {len(steps)}")
    for i, s in enumerate(steps):
        print(f"step {i} [{s.get('action')}] success={s.get('success')}: "
              f"{s.get('result') or s.get('error') or s.get('config')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
