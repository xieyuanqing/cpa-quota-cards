#!/usr/bin/env python3
"""卡片 UI 真机验证:列表渲染 / 进度条 / 点击进详情 / 详情完整分析。"""
import os
import sys

from patchright.sync_api import sync_playwright

SHOTS = os.environ.get("CPA_QUOTA_CARDS_SHOTS", "/root/cpa-quota-cards/shots")
BASE = os.environ.get("CPA_QUOTA_CARDS_URL", "http://127.0.0.1:18390/usage/")


def mgmt_key() -> str:
    with open("/opt/cpa-manager-plus/.env") as fh:
        for line in fh:
            if line.startswith("CPAMP_ADMIN_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未找到 CPAMP_ADMIN_KEY")


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    key = mgmt_key()
    problems: list[str] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir="/opt/browser-automation/profiles/main",
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append("pageerror: " + str(e)))

        page.goto(BASE, wait_until="domcontentloaded")
        page.evaluate("k => localStorage.setItem('cpa-usage-key', k)", key)
        page.reload(wait_until="domcontentloaded")

        page.wait_for_selector(".acct", timeout=30000)
        page.wait_for_timeout(1200)
        cards = page.locator(".acct")
        n = cards.count()
        print(f"卡片数: {n}")
        for i in range(n):
            c = cards.nth(i)
            print("  -", c.get_attribute("data-provider"), "|",
                  " / ".join(c.inner_text().split("\n")[:3]).replace("\n", " ")[:90])
        print("进度条条数:", page.locator(".meter-fill").count())
        page.screenshot(path=f"{SHOTS}/01-list.png", full_page=True)

        # --- 列表态 DOM 断言(进度条宽度 / 横向溢出) ---
        bars = page.evaluate("""() => [...document.querySelectorAll('.meter-fill')].map(el => ({
            w: el.getBoundingClientRect().width / (el.parentElement.getBoundingClientRect().width || 1),
            tone: el.dataset.tone || ''}))""")
        for b in bars:
            print(f"    进度条 填充={b['w'] * 100:.1f}% tone={b['tone']}")
        print("    横向溢出(px):", page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"))

        # 详情:Claude
        page.locator(".acct[data-provider='claude']").click()
        page.wait_for_selector("#detail .dt-head", timeout=20000)
        page.wait_for_timeout(1500)
        print("详情 hash:", page.evaluate("location.hash"))
        print("详情标题:", page.locator("#detail h2").first.inner_text())
        print("详情区块数:", page.locator("#detail .sec").count())

        # --- DOM 断言 ---
        for kw in ("额度预测", "预计花费", "容量反推", "模型构成", "成功率"):
            print(f"    详情含「{kw}」:", page.locator(f"#detail :text('{kw}')").count() > 0)

        page.screenshot(path=f"{SHOTS}/02-detail-claude.png", full_page=True)
        try:
            print("详情预览:", page.locator("#detail").inner_text()[:260].replace("\n", " | "))
        except Exception as exc:  # noqa: BLE001
            problems.append("读详情文本失败: " + str(exc))

        # 返回 + Codex 详情
        page.click("#back")
        page.wait_for_timeout(600)
        page.locator(".acct[data-provider='codex']").click()
        page.wait_for_selector("#detail .dt-head", timeout=20000)
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{SHOTS}/03-detail-codex.png", full_page=True)

        ctx.close()
    print("控制台问题:", len(problems))
    for prob in problems[:12]:
        print("   !", prob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
