#!/usr/bin/env python3
"""Prove the plugin page follows the CPAMP panel's language choice.

Sets the panel's own language store (localStorage cli-proxy-language), reloads the
panel for real, opens the plugin page from the sidebar and asserts on the embedded
document: title, summary, footer, detail sections — in both zh-CN and en.
The panel key is read from .env and never printed.
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

CASES = {
    "en": {
        "title": "Quota & usage",
        "summary": "accounts",
        "section": "Model mix",
        "detail": ["Quota forecast", "Projected spend", "Capacity back-calculation",
                   "Model mix", "Success rate"],
        "html_lang": "en",
    },
    "zh-CN": {
        "title": "额度与用量",
        "summary": "个账号",
        "section": "模型构成",
        "detail": ["额度预测", "预计花费", "容量反推", "模型构成", "成功率"],
        "html_lang": "zh-CN",
    },
}
CJK = [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)]


def has_cjk(text):
    return any(any(lo <= ord(ch) <= hi for lo, hi in CJK) for ch in text)


def panel_key():
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("CPAMP_ADMIN_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("CPAMP_ADMIN_KEY not found in " + str(ENV_FILE))


def main():
    problems = []
    result = {"cases": {}}
    failures = []
    with tempfile.TemporaryDirectory(prefix="cpa-quota-cards-i18n-") as profile:
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
            page.evaluate("() => { const i = document.querySelector('input[type=checkbox]');"
                          " if (i && !i.checked) i.click(); }")
            page.get_by_role("button", name="Login", exact=True).click()
            page.get_by_text(MENU, exact=True).first.wait_for(timeout=30000)

            for code, want in CASES.items():
                # Point the panel's own language store at the code under test.
                page.evaluate(
                    "c => localStorage.setItem('cli-proxy-language',"
                    " JSON.stringify({state:{language:c},version:0}))", code)
                page.reload(wait_until="domcontentloaded")
                link = page.get_by_text(MENU, exact=True).first
                link.wait_for(timeout=30000)
                link.click()

                page.locator("iframe").first.wait_for(timeout=30000)
                frame = page.frame_locator("iframe").first
                frame.locator(".acct").first.wait_for(timeout=30000)
                page.wait_for_timeout(1200)

                inner = next((f for f in page.frames
                              if "resource" in f.url or "usage" in f.url), None)
                case = {
                    "stored_language": page.evaluate(
                        "() => JSON.parse(localStorage.getItem('cli-proxy-language')"
                        " || '{}').state?.language || null"),
                    "iframe_document_lang": inner.evaluate(
                        "() => document.documentElement.lang") if inner else None,
                    "title": frame.locator("#title").inner_text(),
                    "summary": frame.locator("#summary").inner_text(),
                    "srcpill": frame.locator("#srcpill").inner_text(),
                    "stamp": frame.locator("#stamp").inner_text(),
                    "lang_button": frame.locator("#lang").inner_text(),
                    "footer": frame.locator("#foot").inner_text(),
                    "section_titles": frame.locator(".sec-title").all_inner_texts(),
                }

                # Pick a card that actually carries quota windows: an account whose
                # upstream quota source is rate-limited renders without them.
                probe_kw = want["detail"][0]
                used_card = None
                for idx in range(min(3, frame.locator(".acct").count())):
                    frame.locator(".acct").nth(idx).click()
                    frame.locator("#detail .dt-head").first.wait_for(timeout=20000)
                    page.wait_for_timeout(1000)
                    if frame.locator(f"#detail :text('{probe_kw}')").count():
                        used_card = idx
                        break
                    frame.locator("#back").click()
                    page.wait_for_timeout(800)
                case["card_index"] = used_card
                page.wait_for_timeout(400)
                case["detail_keys"] = {
                    kw: frame.locator(f"#detail :text('{kw}')").count() > 0
                    for kw in want["detail"]}
                case["detail_has_cjk"] = has_cjk(frame.locator("#detail").inner_text())
                case["chrome_has_cjk"] = has_cjk(" ".join([
                    case["title"], case["summary"], case["footer"], case["srcpill"]]))
                result["cases"][code] = case

                if case["title"] != want["title"]:
                    failures.append(f"[{code}] title is {case['title']!r}, want {want['title']!r}")
                if want["summary"] not in case["summary"]:
                    failures.append(f"[{code}] summary is {case['summary']!r}")
                if case["iframe_document_lang"] != want["html_lang"]:
                    failures.append(f"[{code}] document lang is {case['iframe_document_lang']!r}")
                missing = [k for k, ok in case["detail_keys"].items() if not ok]
                if missing:
                    failures.append(f"[{code}] detail is missing {missing}")
                if code == "en" and case["detail_has_cjk"]:
                    failures.append("[en] detail view still shows Chinese text")
                if code == "en" and case["chrome_has_cjk"]:
                    failures.append("[en] header/footer still show Chinese text")
                if code == "zh-CN" and not case["chrome_has_cjk"]:
                    failures.append("[zh-CN] header/footer are not Chinese")

            # The toolbar button must flip the language in place, without a reload.
            frame = page.frame_locator("iframe").first
            before = frame.locator("#title").inner_text()
            frame.locator("#lang").click()
            page.wait_for_timeout(600)
            after = frame.locator("#title").inner_text()
            result["toolbar_toggle"] = {"before": before, "after": after,
                                        "button": frame.locator("#lang").inner_text()}
            if after == before:
                failures.append("[toolbar] toggle did not switch the language")
            frame.locator("#lang").click()
            page.wait_for_timeout(600)
            back = frame.locator("#title").inner_text()
            result["toolbar_toggle"]["back"] = back
            if back != before:
                failures.append("[toolbar] toggle did not switch back")
            ctx.close()

    result["console_problems"] = problems[:10]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if failures:
        print("FAILED: " + "; ".join(failures))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
