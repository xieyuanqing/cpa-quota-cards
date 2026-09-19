#!/usr/bin/env python3
"""Capture the screenshots used in the README, with every account identifier masked.

The page is opened standalone on the same origin as the panel (so it still reads the
panel session and the panel's language), rendered, then all identifying text is
replaced in the DOM *before* the shot is taken. Masking is asserted: the script fails
if an e-mail, account id or the raw upstream identifier is still visible.

    CPAMP_PANEL_URL=https://<your-panel-host>/management.html \
        python3 scripts/make_readme_shots.py
"""
import json
import os
import pathlib
import re
import sys
import tempfile

from patchright.sync_api import sync_playwright

PANEL = os.environ.get("CPAMP_PANEL_URL", "https://cliproxy.nijikit.com/management.html")
ORIGIN = os.environ.get("CPAMP_ORIGIN") or re.match(r"https?://[^/]+", PANEL).group(0)
PAGE_URL = ORIGIN + os.environ.get("CPA_QUOTA_CARDS_PATH", "/usage/")
OUT = pathlib.Path(os.environ.get("SHOT_DIR", "docs"))
SHOT_WIDTH = int(os.environ.get("SHOT_WIDTH", "1400"))
ENV_FILE = pathlib.Path(os.environ.get("CPAMP_ENV_FILE", "/opt/cpa-manager-plus/.env"))
MENU = os.environ.get("CPA_QUOTA_CARDS_MENU", "额度与用量")
LANGS = [s for s in os.environ.get("SHOT_LANGS", "en,zh-CN").split(",") if s]

CJK = [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)]

# Replaces identifying text in place: e-mails, UUIDs, long hex ids.
MASK_JS = r"""
() => {
  const MAIL = 'a\u2022\u2022\u2022@\u2022\u2022\u2022.com';
  const mask = s => s
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, MAIL)
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022')
    .replace(/\b(?=[0-9a-f]*[a-f])[0-9a-f]{8}\b/g, '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022')
    .replace(/\b(?=[0-9a-f]*[a-f])[0-9a-f]{36}\b/g, '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022');
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n, hits = 0;
  while ((n = walker.nextNode())) {
    const v = mask(n.nodeValue);
    if (v !== n.nodeValue) { n.nodeValue = v; hits++; }
  }
  for (const el of document.querySelectorAll('[title],[placeholder]')) {
    for (const a of ['title', 'placeholder']) {
      const cur = el.getAttribute(a);
      if (cur) { const v = mask(cur); if (v !== cur) { el.setAttribute(a, v); hits++; } }
    }
  }
  return {hits: hits, text: document.body.innerText};
}
"""


def panel_key():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("CPAMP_ADMIN_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("CPAMP_ADMIN_KEY not found in " + str(ENV_FILE))


def has_cjk(text):
    return [ch for ch in text if any(lo <= ord(ch) <= hi for lo, hi in CJK)]


def shrink(path, width):
    """Downscale a retina capture so the repo stays small (PIL optional)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(path) as im:
        if im.width <= width:
            return im.width
        im.resize((width, round(im.height * width / im.width)),
                  Image.LANCZOS).save(path, optimize=True)
    return width


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"shots": [], "problems": []}
    still_visible = []

    with tempfile.TemporaryDirectory(prefix="cpa-quota-cards-shots-") as profile:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                profile, channel="chrome", headless=False,
                viewport={"width": 1280, "height": 1000},
                device_scale_factor=2,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Log in so the standalone page can reuse the session (same origin).
            page.goto(PANEL, wait_until="domcontentloaded")
            page.locator("input[type=password]").first.wait_for(timeout=30000)
            page.locator("input[type=password]").first.fill(panel_key())
            page.evaluate("() => { const i = document.querySelector('input[type=checkbox]');"
                          " if (i && !i.checked) i.click(); }")
            page.get_by_role("button", name="Login", exact=True).click()
            page.get_by_text(MENU, exact=True).first.wait_for(timeout=30000)
            report["panel_theme"] = page.evaluate(
                "() => localStorage.getItem('cli-proxy-theme')")
            report["panel_language"] = page.evaluate(
                "() => localStorage.getItem('cli-proxy-language')")

            for code in LANGS:
                page.evaluate(
                    "c => localStorage.setItem('cli-proxy-language',"
                    " JSON.stringify({state:{language:c},version:0}))", code)
                page.goto(PAGE_URL, wait_until="domcontentloaded")
                page.locator(".acct").first.wait_for(timeout=30000)
                page.wait_for_timeout(1500)

                if code == "en":
                    chrome = page.evaluate(
                        "() => document.getElementById('title').innerText + '\\n'"
                        " + document.getElementById('summary').innerText + '\\n'"
                        " + document.getElementById('srcpill').innerText + '\\n'"
                        " + document.getElementById('foot').innerText")
                    if has_cjk(chrome):
                        report["problems"].append(
                            "EN chrome still contains CJK: " + "".join(has_cjk(chrome)))

                masked = page.evaluate(MASK_JS)
                bad = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                                 masked["text"])
                if bad:
                    report["problems"].append("unmasked e-mail in list view: " + str(bad))
                path = OUT / f"quota-cards-{code.split('-')[0]}.png"
                page.screenshot(path=str(path), full_page=True)
                report["shots"].append({"file": str(path), "lang": code,
                                        "masked_nodes": masked["hits"],
                                        "width": shrink(path, SHOT_WIDTH),
                                        "bytes": path.stat().st_size})

            # Detail view of the richest account (most quota windows).
            accounts = page.evaluate(
                "async (key) => { const r = await fetch('/usage/api/accounts',"
                " {headers: {Authorization: 'Bearer ' + key}});"
                " const d = await r.json();"
                " return (d.accounts || []).map(a => ({id: a.id, account: a.account,"
                " raw: a.account_raw, wins: (a.windows || []).length, err: a.error || null})); }",
                panel_key())
            report["accounts"] = [{k: v for k, v in a.items() if k in ("id", "wins", "err")}
                                  for a in accounts]
            pick = max(accounts, key=lambda a: a["wins"], default=None)
            if pick is None:
                report["problems"].append("no accounts to shoot a detail view for")
            else:
                report["detail_account"] = pick["id"]
                page.goto(PAGE_URL + "#acct=" + pick["id"], wait_until="domcontentloaded")
                page.locator("#detail .sec-title").first.wait_for(timeout=30000)
                page.wait_for_timeout(1500)
                masked = page.evaluate(MASK_JS)
                bad = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                                 masked["text"])
                if bad:
                    report["problems"].append("unmasked e-mail in detail view: " + str(bad))
                for acct in accounts:
                    for field in ("account", "raw"):
                        val = acct.get(field)
                        if val and len(val) > 8 and val in masked["text"]:
                            still_visible.append(f"{acct['id']}.{field}")
                if still_visible:
                    report["problems"].append("identifier still visible: " + str(still_visible))
                path = OUT / "quota-detail-en.png"
                page.screenshot(path=str(path), full_page=False)
                report["shots"].append({"file": str(path), "lang": LANGS[0],
                                        "account": pick["id"],
                                        "masked_nodes": masked["hits"],
                                        "width": shrink(path, SHOT_WIDTH),
                                        "bytes": path.stat().st_size})

            ctx.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["problems"]:
        print("PROBLEMS FOUND", file=sys.stderr)
        return 1
    print("ALL SHOTS MASKED AND CAPTURED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
