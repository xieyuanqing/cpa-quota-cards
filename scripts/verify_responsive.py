#!/usr/bin/env python3
"""Responsive regression for quota cards: every card/detail must stay inside its container."""
import json
import os
import pathlib
import sys
import tempfile

from patchright.sync_api import sync_playwright

BASE = os.environ.get("CPA_QUOTA_CARDS_URL", "https://cliproxy.nijikit.com/usage/")
SHOTS = pathlib.Path(os.environ.get("CPA_QUOTA_CARDS_SHOTS", "/root/cpa-quota-cards/shots"))
WIDTHS = (320, 360, 390, 590, 770, 1230, 1440)


def panel_key() -> str:
    for line in pathlib.Path("/opt/cpa-manager-plus/.env").read_text().splitlines():
        if line.startswith("CPAMP_ADMIN_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("CPAMP_ADMIN_KEY not found")


MEASURE = """rootSelector => {
  const root=document.querySelector(rootSelector);
  const rr=root.getBoundingClientRect();
  const tol=1;
  const bad=[...root.querySelectorAll('*')].map(el=>{
    const r=el.getBoundingClientRect();
    return {tag:el.tagName,cls:String(el.className||'').slice(0,60),id:el.id||'',left:r.left,right:r.right,width:r.width};
  }).filter(x=>x.width>0 && (x.left < rr.left-tol || x.right > rr.right+tol));
  const cards=[...root.querySelectorAll('.card')].map(el=>{
    const r=el.getBoundingClientRect();
    return {left:r.left,right:r.right,width:r.width};
  });
  return {
    viewport:document.documentElement.clientWidth,
    scrollWidth:document.documentElement.scrollWidth,
    root:{left:rr.left,right:rr.right,width:rr.width},
    cards,
    offenders:bad.slice(0,12)
  };
}"""


def healthy(m: dict) -> bool:
    if m["scrollWidth"] > m["viewport"] + 1 or m["offenders"]:
        return False
    left, right = m["root"]["left"], m["root"]["right"]
    return all(c["left"] >= left - 1 and c["right"] <= right + 1 for c in m["cards"])


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    results=[]
    failures=[]
    with tempfile.TemporaryDirectory(prefix="cpa-quota-responsive-") as profile:
        with sync_playwright() as pw:
            ctx=pw.chromium.launch_persistent_context(
                profile,channel="chrome",headless=False,
                viewport={"width":1440,"height":960},
                args=["--no-sandbox","--disable-dev-shm-usage"])
            page=ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(BASE,wait_until="domcontentloaded")
            page.evaluate("k => localStorage.setItem('cpa-usage-key',k)",panel_key())
            page.reload(wait_until="domcontentloaded")
            page.locator(".acct").first.wait_for(timeout=30000)

            for width in WIDTHS:
                page.set_viewport_size({"width":width,"height":900})
                page.goto(BASE,wait_until="domcontentloaded")
                page.locator(".acct").first.wait_for(timeout=30000)
                page.wait_for_timeout(250)
                listing=page.evaluate(MEASURE,"#accounts")
                list_ok=healthy(listing)

                page.locator(".acct").first.click()
                page.locator("#detail .dt-head").first.wait_for(timeout=30000)
                page.wait_for_timeout(250)
                detail=page.evaluate(MEASURE,"#detail")
                detail_ok=healthy(detail)
                row={"width":width,"list_ok":list_ok,"detail_ok":detail_ok,
                     "list":listing,"detail":detail}
                results.append(row)
                if not (list_ok and detail_ok):
                    failures.append(width)
                    page.screenshot(path=str(SHOTS/f"responsive-fail-{width}.png"),full_page=True)

            ctx.close()
    print(json.dumps(results,ensure_ascii=False,indent=2))
    if failures:
        print("FAILED widths:",", ".join(map(str,failures)))
        return 1
    print("ALL RESPONSIVE ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
