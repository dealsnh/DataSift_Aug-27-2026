"""Run DataSift's Enrich Property Data + Skip Trace, scoped to an exact tag set
rather than a whole shared list. See `enrich_records`/`skip_trace_records` in
`datasift_uploader.py` for why: a bare list name (e.g. "Foreclosure") holds
every record ever uploaded to that list account-wide, not just one day's pull.

    python src/run_enrich_skiptrace_by_tags.py --tags "Priority 2,FTM,pulled_2026-09-04" --headed
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_enrich_skiptrace_by_tags")


async def main_async(tags: list[str], headed: bool, skip_enrich: bool, skip_trace: bool) -> int:
    from datasift_core import create_browser, load_cookies, login, save_cookies
    from datasift_uploader import enrich_records, skip_trace_records

    if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
        logger.error("DATASIFT_EMAIL / DATASIFT_PASSWORD not set")
        return 2

    async with create_browser(headless=not headed) as (browser, context, page):
        await load_cookies(context)
        if not await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD):
            logger.error("DataSift login failed")
            return 2
        await save_cookies(page)

        failures = 0

        if not skip_enrich:
            logger.info("=== enrich, tags=%s ===", tags)
            try:
                res = await enrich_records(page, tags=tags)
            except Exception as exc:
                logger.exception("Enrichment crashed")
                res = {"success": False, "message": f"{type(exc).__name__}: {exc}"}
            status = "OK" if res.get("success") else "FAILED"
            logger.info("[%s] enrich: %s", status, res.get("message", ""))
            if not res.get("success"):
                failures += 1

        if not skip_trace:
            logger.info("=== skip trace, tags=%s ===", tags)
            try:
                res = await skip_trace_records(page, tags=tags)
            except Exception as exc:
                logger.exception("Skip trace crashed")
                res = {"success": False, "message": f"{type(exc).__name__}: {exc}"}
            status = "OK" if res.get("success") else "FAILED"
            logger.info("[%s] skip trace: %s", status, res.get("message", ""))
            if not res.get("success"):
                failures += 1

        return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", required=True,
                    help="Comma-separated tags, ALL must match (AND)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--no-skip-trace", action="store_true")
    a = ap.parse_args()
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    if not tags:
        print("No tags given.")
        return 2
    return asyncio.run(main_async(tags, a.headed, a.no_enrich, a.no_skip_trace))


if __name__ == "__main__":
    raise SystemExit(main())
