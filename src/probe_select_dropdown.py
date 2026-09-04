"""One-shot diagnostic: find the real "select all / Select Max" dropdown on the
Records page, so enrich/skip trace can cover the whole filtered set instead of
just the 10 rows rendered on page one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    stream=sys.stdout)


async def main_async() -> int:
    from datasift_core import create_browser, load_cookies, login, save_cookies
    from datasift_uploader import _filter_by_tags, _dismiss_popups

    async with create_browser(headless=True) as (browser, context, page):
        await load_cookies(context)
        if not await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD):
            print("login failed")
            return 2
        await save_cookies(page)

        ok = await _filter_by_tags(page, ["FTM", "foreclosure", "pulled_2026-09-04"])
        print("filter applied:", ok)
        await page.wait_for_timeout(2000)
        await _dismiss_popups(page)

        # What does the header selection area actually look like?
        info = await page.evaluate("""() => {
            const out = {classHits: [], nearHeaderCheckbox: [], svgSiblings: []};
            document.querySelectorAll('[class]').forEach(el => {
              const c = el.className;
              if (typeof c === 'string' && /checkbox|Checkbox|dropdown|Dropdown/.test(c)) {
                const r = el.getBoundingClientRect();
                if (r.width && r.height && r.top < 400) {
                  out.classHits.push({cls: c, x: Math.round(r.x), y: Math.round(r.y),
                                      w: Math.round(r.width), h: Math.round(r.height),
                                      tag: el.tagName});
                }
              }
            });
            const cbs = [...document.querySelectorAll('input[type=checkbox]')]
              .filter(cb => !cb.classList.contains('react-toggle-screenreader-only'));
            if (cbs.length) {
              const first = cbs[0].getBoundingClientRect();
              document.querySelectorAll('*').forEach(el => {
                const r = el.getBoundingClientRect();
                if (Math.abs(r.top - first.top) < 25 && r.left > first.left
                    && r.left < first.left + 120 && r.width < 60 && r.width > 3) {
                  out.nearHeaderCheckbox.push({tag: el.tagName,
                    cls: (typeof el.className === 'string' ? el.className : ''),
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height)});
                }
              });
            }
            return out;
        }""")
        print("\n=== class hits (checkbox/dropdown, top area) ===")
        for h in info["classHits"][:25]:
            print(" ", h)
        print("\n=== elements just right of the header checkbox (the caret?) ===")
        for h in info["nearHeaderCheckbox"][:25]:
            print(" ", h)

        # Try clicking the caret and see what menu text appears
        cbs = page.locator('input[type="checkbox"]')
        if await cbs.count() > 0:
            box = await cbs.first.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] + 14, box["y"] + box["height"] / 2)
                await page.wait_for_timeout(1500)
                menu = await page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('*').forEach(el => {
                      if (el.children.length === 0) {
                        const t = (el.textContent || '').trim();
                        if (/Select/i.test(t) && t.length < 60) {
                          const r = el.getBoundingClientRect();
                          if (r.width && r.height)
                            out.push({text: t, cls: (typeof el.className === 'string' ? el.className : ''),
                                      x: Math.round(r.x), y: Math.round(r.y)});
                        }
                      }
                    });
                    return out;
                }""")
                print("\n=== menu items containing 'Select' after clicking the caret ===")
                for m in menu[:25]:
                    print(" ", m)
                await page.screenshot(path="datasift_probe_select_menu.png")
                print("\nscreenshot: datasift_probe_select_menu.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
