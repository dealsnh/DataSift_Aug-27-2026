"""Diagnostic v4: does the per-record DETAIL view carry a property address / APN /
legal description, or only document-image metadata? Calls
detailsContainer.getDetails() directly (same trick as probe2's direct
search.Search()) for one known record (doc 2026000247324, docid 31818917, from the
first 60-record pull) and inspects the DetailsPresentor.aspx response.
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
setVal('MainContent_MainMenu1_SearchByDocType1_FromDate','08/01/2026');
setVal('MainContent_MainMenu1_SearchByDocType1_ToDate','09/03/2026');
return 'dates-set';
"""

DIRECT_SEARCH = """
var q = '&FromDate=08/01/2026&ToDate=09/03/2026&DocumentTypes=210,&DocumentNames=NT TRUSTEE SALE,&ERetrievalGroup=1&SearchMode=3&IsNewSearch=true';
search.Search(q, null, null);
return 'direct-search-fired';
"""

GET_DETAILS = """
var data = '&ImgIsPCOR=False&ImgIsDTT=False&ImgIsOBIndex=False&ImgIsOBIndexCell=False&OBBookTab=&OBBookSeq=&OBIndexPage=&OBIndexCell=&OBDocImageBook=&OBDocImagePage=&OBDocImageType=&OBDocImageRecYear=&OBDocImageFormType=&ImgIsRef=False&FromBasket=False&FitToSize=False&ERetrievalGroup=1&IsNewSearch=True&resultsCount=80&docIdIndex=0&imgIndex=1&docid=31818917&ImgIsOBDocImage=False';
detailsContainer.getDetails(data);
return 'get-details-fired';
"""

READ_DETAILS_DOM = """
var el = document.getElementById('detailsPage') || document.querySelector('.docDetailsContainer');
return JSON.stringify({
  found: !!el,
  html: el ? el.innerHTML.slice(0, 4000) : null,
  bodyHasDetails: document.body.innerHTML.indexOf('DetailsPresentor') !== -1
});
"""


def main() -> int:
    client = ScrapflyClient(key=config.SCRAPFLY_KEY)
    resp = client.scrape(ScrapeConfig(
        url=URL, render_js=True, asp=True, country=config.SCRAPFLY_COUNTRY,
        proxy_pool="public_residential_pool", rendering_wait=1000,
        js_scenario=[
            {"wait_for_selector": {"selector": DOCTYPE_TAB_LINK, "timeout": 15000}},
            {"click": {"selector": DOCTYPE_TAB_LINK}},
            {"wait": 800},
            {"execute": {"script": CHECK_AND_READ}},
            {"execute": {"script": SET_DATES}},
            {"execute": {"script": DIRECT_SEARCH}},
            {"wait": 7000},
            {"execute": {"script": GET_DETAILS}},
            {"wait": 5000},
            {"execute": {"script": READ_DETAILS_DOM}},
        ],
        raise_on_upstream_error=False,
    ))
    sr = resp.scrape_result
    print("status:", sr.get("status_code"), "success:", sr.get("success"), "error:", sr.get("error"))
    bd = sr.get("browser_data") or {}
    steps = ((bd.get("js_scenario") or {}).get("steps")) or []
    for i, s in enumerate(steps):
        if s.get("action") == "execute":
            print(f"\nstep {i}: {s.get('result')}")

    xhr = bd.get("xhr_call") or []
    print(f"\n=== xhr_call ({len(xhr)} entries) ===")
    for x in xhr:
        if "DetailsPresentor" in (x.get("url") or ""):
            print("URL:", x.get("url"))
            print("STATUS:", x.get("response", {}).get("status"))
            print("RESPONSE BODY:\n", x.get("response", {}).get("body"))

    Path("output/occa_recorder_probe4_raw.json").write_text(
        json.dumps(bd, indent=2, default=str), encoding="utf-8")
    print("\nwrote output/occa_recorder_probe4_raw.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
