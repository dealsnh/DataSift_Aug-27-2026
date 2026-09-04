"""
Filter resolved OC foreclosure records to Single Family / Multi-Family only.

Property type has to come from a live lookup, not the Recorder or Enformion --
neither carries it. Orange County's own SiftMap client (the one buyer_sweep.py /
comp_package.py use) lives on a DIFFERENT machine's checkout
(`C:\\Users\\Tyrus\\...\\Deal Room Coaching Call\\_api`) and is not present on
this workstation, so this reuses the self-contained OpenWeb Ninja Zillow lookup
`property_enricher._fetch_property()` instead (same API key, no extra
dependency).

Only `SINGLE_FAMILY`, `MULTI_FAMILY`, and `APARTMENT` (Zillow's own bucket for
small multi-unit, mapped to "Multi-Family" everywhere else in this codebase)
pass. `CONDO`, `TOWNHOUSE`, `MANUFACTURED`, `LOT`/`LAND` are dropped, and so is
a record where Zillow has no data at all for the address -- an unconfirmed type
does not get to claim it qualifies.

Usage:
    python src/occa_foreclosure_property_filter.py --in output/occa_foreclosure_pull_20260903_resolved.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import config  # noqa: E402
from property_enricher import _fetch_property, _TYPE_MAP  # noqa: E402

QUALIFYING = {"SINGLE_FAMILY", "MULTI_FAMILY", "APARTMENT"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    recs = data["records"]
    resolved = [r for r in recs if r.get("resolved_address")]
    print(f"{len(resolved)} of {len(recs)} record(s) have an address to check "
          f"property type against")

    kept, dropped = [], []
    for i, r in enumerate(resolved, 1):
        data_pt = _fetch_property(
            r["resolved_address"], r.get("resolved_city", ""), "CA",
            r.get("resolved_zip", ""), config.OPENWEBNINJA_API_KEY,
        )
        home_type = (data_pt or {}).get("homeType", "") or ""
        r["zillow_home_type"] = home_type
        r["property_type"] = _TYPE_MAP.get(home_type.upper(), home_type.title() or "Unknown")
        if home_type.upper() in QUALIFYING:
            kept.append(r)
        else:
            dropped.append(r)
        why = home_type or "no Zillow data"
        print(f"  {i}/{len(resolved)}  {r['resolved_address'][:34]:34} -> "
              f"{why:14} {'KEEP' if home_type.upper() in QUALIFYING else 'drop'}")
        time.sleep(1.2)

    out = args.out or args.inp.replace("_resolved.json", "_filtered.json")
    Path(out).write_text(json.dumps({
        **{k: v for k, v in data.items() if k != "records"},
        "filtered_at": datetime.now().isoformat(timespec="seconds"),
        "property_type_filter": sorted(QUALIFYING),
        "count": len(kept),
        "dropped_count": len(dropped),
        "records": kept,
        "dropped": dropped,
    }, indent=2), encoding="utf-8")

    from collections import Counter
    print(f"\nwrote {len(kept)} qualifying record(s) -> {out} "
          f"({len(dropped)} dropped)")
    print("dropped reasons:", dict(Counter(
        d.get("zillow_home_type") or "no data" for d in dropped)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
