#!/usr/bin/env python3
"""Prove the plugin page really shows up in the CPAMP sidebar and renders.

Logs in to the real panel, clicks the plugin's sidebar entry, then asserts on the
embedded document: same-origin frame, account cards, meters, no key prompt, and a
working detail view. The panel key is read from .env and never printed.
"""
import json
import os
import pathlib
import sys
import tempfile

from patchright.sync_api import sync_playwright

PANEL = os.environ.get("CPAMP_PANEL_URL", "http://127.0.0.1:18317/management.html")
ENV_FILE = pathlib.Path(os.environ.get("CPAMP_ENV_FILE", "/opt/cpa-manager-plus/.env"))
MENU = os.environ.get("CPA_QUOTA_CARDS_MENU", "额度与用量")
SHOTS = pathlib.Path(os.environ.get("CPA_QUOTA_CARDS_SHOTS", "/root/cpa-quota-cards/shots"))

RESPONSIVE_METRICS = """(body, selector) => {
  const root=body.querySelector(selector);
  const rr=root.getBoundingClientRect();
  const cards=[...root.querySelectorAll('.card')].map(el=>{
    const r=el.getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width};
  });
  const offenders=[...root.querySelectorAll('*')].map(el=>{
    const r=el.getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width};
  }).filter(r=>r.width>0 && (r.left<rr.left-1 || r.right>rr.right+1));
  const doc=body.ownerDocument.documentElement;
  return {
    viewport:doc.clientWidth,scrollWidth:doc.scrollWidth,
    root:{left:rr.left,right:rr.right,width:rr.width},cards,
    offender_count:offenders.length,
    ok:doc.scrollWidth<=doc.clientWidth+1 && offenders.length===0 &&
       cards.every(r=>r.left>=rr.left-1 && r.right<=rr.right+1)
  };
}"""


def panel_key():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("CPAMP_ADMIN_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("CPAMP_ADMIN_KEY not found in " + str(ENV_FILE))


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems = []
    result = {}
    with tempfile.TemporaryDirectory(prefix="cpa-quota-cards-panel-") as profile:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                profile, channel="chrome", headless=False,
                viewport={"width": 1440, "height": 960},
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: problems.append("pageerror: " + str(e)))

            page.goto(PANEL, wait_until="domcontentloaded")
            page.locator("input[type=password]").first.wait_for(timeout=30000)
            page.locator("input[type=password]").first.fill(panel_key())
            result["remember_toggled"] = page.evaluate(
                "() => { const i = document.querySelector('input[type=checkbox]');"
                " if (!i) return 'no-checkbox'; const was = i.checked; if (!was) i.click();"
                " return was + '->' + i.checked; }")
            page.get_by_role("button", name="Login", exact=True).click()
            result["panel_login_stored"] = page.evaluate(
                "() => !!localStorage.getItem('cli-proxy-auth')")

            link = page.get_by_text(MENU, exact=True).first
            link.wait_for(timeout=30000)
            result["sidebar_entry"] = link.inner_text()
            result["superseded_entry_present"] = (
                page.get_by_text("额度容量预测", exact=True).count() > 0)
            page.screenshot(path=str(SHOTS / "panel-01-sidebar.png"), full_page=True)
            link.click()

            page.locator("iframe").first.wait_for(timeout=30000)
            frame = page.frame_locator("iframe").first
            frame.locator(".acct").first.wait_for(timeout=30000)

            def subscription_card():
                windowed = frame.locator(".acct:has(.meter-fill)")
                if windowed.count():
                    return windowed.first
                preferred = frame.locator(
                    ".acct[data-provider='claude'], .acct[data-provider='codex']")
                return preferred.first if preferred.count() else frame.locator(".acct").first

            page.wait_for_timeout(1200)

            src = page.locator("iframe").first.get_attribute("src")
            panel_origin = page.evaluate("() => location.origin")
            frame_url = next((f.url for f in page.frames if "resource" in f.url or "usage" in f.url), "")
            result.update({
                "iframe_src": src,
                "panel_origin": panel_origin,
                "same_origin": bool(frame_url) and frame_url.startswith(panel_origin),
                "account_cards": frame.locator(".acct").count(),
                "meters": frame.locator(".meter-fill").count(),
                "key_prompt_visible": frame.locator("#manual:not(.hidden)").count() > 0,
            })
            page.screenshot(path=str(SHOTS / "panel-02-cards.png"), full_page=True)

            subscription_card().click()
            frame.locator("#detail .dt-head").first.wait_for(timeout=20000)
            page.wait_for_timeout(1500)
            result["detail_sections"] = frame.locator("#detail .sec").count()
            for kw in ("额度预测", "预计花费", "容量反推", "模型构成", "成功率"):
                result[f"detail_has_{kw}"] = frame.locator(
                    f"#detail :text('{kw}')").count() > 0
            page.screenshot(path=str(SHOTS / "panel-03-detail.png"), full_page=True)

            result["horizontal_overflow_px"] = page.evaluate(
                "() => document.documentElement.scrollWidth - window.innerWidth")

            responsive = {}
            for width, height in ((390, 844), (590, 960), (980, 844), (1440, 960)):
                page.set_viewport_size({"width": width, "height": height})
                back = frame.locator("#back:not(.hidden)")
                if back.count():
                    back.click()
                frame.locator(".acct").first.wait_for(timeout=30000)
                page.wait_for_timeout(300)
                listing = frame.locator("body").evaluate(RESPONSIVE_METRICS, "#accounts")
                subscription_card().click()
                frame.locator("#detail .dt-head").first.wait_for(timeout=20000)
                page.wait_for_timeout(300)
                detail = frame.locator("body").evaluate(RESPONSIVE_METRICS, "#detail")
                iframe_rect = page.locator("iframe").first.evaluate(
                    "el => { const r=el.getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width}; }")
                responsive[str(width)] = {
                    "iframe": iframe_rect, "list": listing, "detail": detail,
                }
                if width == 390:
                    page.screenshot(path=str(SHOTS / "panel-04-mobile.png"), full_page=True)
            result["responsive"] = responsive
            ctx.close()

    result["console_problems"] = problems[:10]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failures = []
    if result.get("sidebar_entry") != MENU:
        failures.append("sidebar entry missing")
    if result.get("superseded_entry_present"):
        failures.append("the superseded 额度容量预测 entry is still in the sidebar")
    if not result.get("same_origin"):
        failures.append("plugin page is not same-origin with the panel")
    if result.get("account_cards", 0) < 1:
        failures.append("no account cards rendered")
    if result.get("meters", 0) < 2:
        failures.append("quota meters missing")
    if result.get("key_prompt_visible"):
        failures.append("key prompt shown although the panel login state exists")
    if result.get("detail_sections", 0) < 5:
        failures.append("detail view incomplete")
    for width, metrics in result.get("responsive", {}).items():
        if not metrics.get("list", {}).get("ok"):
            failures.append(f"list overflows inside plugin iframe at {width}px")
        if not metrics.get("detail", {}).get("ok"):
            failures.append(f"detail overflows inside plugin iframe at {width}px")
    if failures:
        print("FAILED: " + "; ".join(failures))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
