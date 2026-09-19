#!/usr/bin/env python3
"""CPA 用量速览 —— 只回答两个问题:用了多少、额度还剩多少。

设计口径(与用户约定一致):
  · 消耗 = 每个请求的 token(未缓存输入/输出/缓存读/缓存写)× 官方价格(models.dev)
  · 额度 = 上游直接给的百分比(Claude 走 OAuth 用量接口,Codex 走响应头),每次请求后刷新
  · 不做"预计耗尽时间";容量靠历史采样反推,跑得越多越准
  · 不含置信区间等花活

数据来源(全部本地读取,不改动任何东西):
  1. CPAMP 数据库(只读)   : 每请求 token + 官方价格表(4917 条)
  2. Anthropic OAuth 用量接口: Claude 订阅 5h/周额度利用率
  3. CPAMP 额度快照         : Codex 5h/周额度(被动,随响应头)

用法:
  python3 cpa_usage.py            # 人类可读
  python3 cpa_usage.py --json     # 机器可读
  python3 cpa_usage.py --no-log   # 不回写采样历史
"""
from __future__ import annotations

import binascii
import glob
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


def _base_url(name, default):
    """Read a base URL and drop any path, so a stray API prefix cannot leak in."""
    value = os.environ.get(name, default).strip().rstrip("/")
    parts = urlsplit(value)
    if parts.path and parts.path != "/":
        value = "{}://{}".format(parts.scheme, parts.netloc)
    return value


HERE = os.path.dirname(os.path.abspath(__file__))
CPAMP_DB = os.environ.get("CPAMP_DB", "/opt/cpa-manager-plus/data/usage.sqlite")
CPAMP_ENV = os.environ.get("CPAMP_ENV_FILE", "/opt/cpa-manager-plus/.env")
CLAUDE_AUTH_GLOB = os.environ.get(
    "CPA_CLAUDE_AUTH_GLOB", "/root/CLIProxyAPI/auths/claude-*.json")
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CPA_BASE = _base_url("CPA_BASE_URL", "http://127.0.0.1:8317")
STATE_PATH = os.environ.get("CPA_USAGE_STATE", os.path.join(HERE, "state.jsonl"))
CST = timezone(timedelta(hours=8))
HEXSEG = re.compile(r"^[0-9a-fA-F]{8,}$")

# 窗口跨度(毫秒),用于在缺少 cycle_start 时回推窗口起点
SPAN_MS = {"five_hour": 5 * 3600 * 1000, "weekly": 7 * 24 * 3600 * 1000}


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def fmt_cst(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%m-%d %H:%M") if ms else "-"


def left_str(ms, ref) -> str:
    if not ms:
        return ""
    secs = max(0, int((ms - ref) / 1000))
    h, m = divmod(secs // 60, 60)
    return ("%dh%02dm" % (h, m)) if h else ("%dm" % m)


def fmt_tok(n) -> str:
    n = n or 0
    if n >= 1_000_000:
        return "%.2fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.1fK" % (n / 1_000)
    return str(n)


def fmt_usd(v) -> str:
    return "$%.2f" % (v or 0)


def connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=15)


def decode_account_key(key: str) -> str:
    out = []
    for seg in (key or "").split(":"):
        if HEXSEG.match(seg):
            try:
                dec = binascii.unhexlify(seg).decode("utf-8")
                if dec.isprintable():
                    out.append(dec)
                    continue
            except Exception:  # noqa: BLE001
                pass
        out.append(seg)
    return "/".join(out)


def load_prices(con: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in con.execute(
        "select model, prompt_per_1m, completion_per_1m,"
        " coalesce(cache_read_per_1m, cache_per_1m), cache_creation_per_1m from model_prices"
    ):
        model, p, c, cr, cc = row
        out[model.lower()] = {"in": p or 0.0, "out": c or 0.0,
                              "cache_read": cr or 0.0, "cache_creation": cc or 0.0}
    return out


def price_of(prices: dict[str, dict], model: str | None):
    if not model:
        return None
    m = model.lower()
    if m in prices:
        return prices[m]
    parts = m.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and parts[0] in prices:
        return prices[parts[0]]
    return None


def usage_window(con, prices, provider: str, start_ms: int, end_ms: int | None = None) -> dict:
    """窗口内某 provider 的请求数 / 分类 token / 按官方价格的费用。

    统一用 CPAMP 的 normalized_* 字段,避免 OpenAI 语义下 input 含缓存导致重复计费:
      费用 = 未缓存输入×输入价 + 缓存读×缓存读价 + 缓存写×缓存写价 + 输出×输出价
    """
    sql = ("select model, count(*),"
           " coalesce(sum(normalized_uncached_input_tokens), 0),"
           " coalesce(sum(normalized_cache_read_tokens), 0),"
           " coalesce(sum(cache_creation_tokens), 0),"
           " coalesce(sum(output_tokens), 0),"
           " coalesce(sum(total_tokens), 0),"
           " coalesce(sum(case when failed=1 then 1 else 0 end), 0)"
           " from usage_events where provider=? and timestamp_ms>=?")
    args: list = [provider, start_ms]
    if end_ms:
        sql += " and timestamp_ms<?"
        args.append(end_ms)
    sql += " group by model"

    agg = {"calls": 0, "failed": 0, "in": 0, "cache_read": 0, "cache_creation": 0,
           "out": 0, "total": 0, "cost": 0.0, "unpriced": [], "models": []}
    for model, calls, tin, cr, cc, tout, total, failed in con.execute(sql, args):
        pr = price_of(prices, model)
        cost = 0.0
        if pr:
            cost = ((tin / 1e6) * pr["in"] + (cr / 1e6) * pr["cache_read"]
                    + (cc / 1e6) * pr["cache_creation"] + (tout / 1e6) * pr["out"])
        elif calls:
            agg["unpriced"].append(model)
        agg["calls"] += calls
        agg["failed"] += failed
        agg["in"] += tin
        agg["cache_read"] += cr
        agg["cache_creation"] += cc
        agg["out"] += tout
        agg["total"] += total
        agg["cost"] += cost
        if calls:
            agg["models"].append({"model": model, "calls": calls, "cost": round(cost, 4)})
    agg["cost"] = round(agg["cost"], 2)
    agg["models"].sort(key=lambda x: -x["cost"])
    return agg


def _cpa_mgmt_key() -> str | None:
    if os.environ.get("CPA_MANAGEMENT_KEY"):
        return os.environ["CPA_MANAGEMENT_KEY"]
    try:
        with open(CPAMP_ENV) as fh:
            for line in fh:
                if line.startswith("CPA_MANAGEMENT_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _refresh_claude_token() -> bool:
    key = _cpa_mgmt_key()
    if not key:
        return False
    req = urllib.request.Request(
        "%s/v0/management/auth-files/refresh" % CPA_BASE, data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except Exception:  # noqa: BLE001
        return False


def claude_quota() -> dict:
    files = sorted(glob.glob(CLAUDE_AUTH_GLOB))
    if not files:
        return {"error": "找不到 Claude auth 文件"}

    def call():
        with open(files[0]) as fh:
            auth = json.load(fh)
        req = urllib.request.Request(
            CLAUDE_USAGE_URL,
            headers={"Authorization": "Bearer " + auth.get("access_token", ""),
                     "anthropic-beta": "oauth-2025-04-20",
                     "User-Agent": "claude-cli/2.1.0 (external, cli)",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return auth, json.loads(resp.read().decode())

    try:
        auth, data = call()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403) and _refresh_claude_token():
            try:
                auth, data = call()
            except Exception as exc2:  # noqa: BLE001
                return {"error": "刷新 token 后仍失败: %s" % exc2}
        else:
            return {"error": "HTTP %s" % exc.code}
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}

    def win(key):
        blk = data.get(key) or {}
        if blk.get("utilization") is None:
            return None
        reset = blk.get("resets_at")
        return {"used_percent": blk["utilization"],
                "reset_at_ms": int(datetime.fromisoformat(reset).timestamp() * 1000) if reset else None}

    return {"account": auth.get("email"), "five_hour": win("five_hour"),
            "weekly": win("seven_day"), "extra_usage": data.get("extra_usage"),
            "limits": data.get("limits") or []}


def codex_quota(con) -> list[dict]:
    rows = con.execute(
        "select account_key, window_kind, used_percent, plan_type, source,"
        " coalesce(cycle_start_ms, cycle_end_ms - case window_kind"
        "   when 'five_hour' then 18000000 else 604800000 end),"
        " cycle_end_ms, observed_at_ms from account_quota_snapshots"
        " where provider='codex' and window_kind in ('five_hour','weekly')"
        " order by observed_at_ms desc limit 400").fetchall()
    seen: dict[tuple, dict] = {}
    for acct, kind, used, plan, source, start, end, obs in rows:
        seen.setdefault((acct, kind), {
            "account_key": acct, "account": decode_account_key(acct), "window_kind": kind,
            "used_percent": used, "plan": plan, "source": source,
            "start_ms": start, "end_ms": end, "observed_at_ms": obs})
    return list(seen.values())


def load_state() -> list[dict]:
    out = []
    try:
        with open(STATE_PATH) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return out


def append_state(samples: list[dict]) -> None:
    """追加采样;文件过大时按 30 天窗口裁剪重写,避免无限增长。"""
    try:
        line_count = 0
        with open(STATE_PATH) as fh:
            line_count = sum(1 for _ in fh)
        if line_count > 20000:
            cutoff = now_ms() - 30 * 24 * 3600 * 1000
            kept = [s for s in load_state() if s.get("ts", 0) >= cutoff]
            with open(STATE_PATH, "w") as fh:
                for s in kept:
                    fh.write(json.dumps(s, ensure_ascii=False) + "\n")
        with open(STATE_PATH, "a") as fh:
            for s in samples:
                fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    except OSError:
        pass


def append_sample(provider, kind, window_start_ms, used_percent, cost, calls):
    """每个(provider, 窗口)只保留每个观测时刻一条;同一窗口的连续样本用于反推容量。"""
    if used_percent is None or not window_start_ms:
        return None
    return {"ts": now_ms(), "provider": provider, "kind": kind,
            "window_start_ms": int(window_start_ms), "used_percent": float(used_percent),
            "cost": float(cost or 0.0), "calls": int(calls or 0)}


def capacity_from_history(history: list[dict], provider: str, kind: str,
                          cur_start=None, cur_used=None, cur_cost=None) -> dict:
    """从历史样本反推「100% 额度 ≈ 多少美元等效」。

    做法:同一额度窗口内取连续样本,累加 Δ费用 与 Δ百分点,用 总Δ费用/总Δ百分点
    作为「每 1% 多少钱」(过原点的最小二乘斜率,比逐段取比值稳健得多);
    再按窗口给出离散度 —— 采样越多、窗口越多,估计越稳。
    """
    groups: dict[int, list[dict]] = {}
    for s in history:
        if s.get("provider") == provider and s.get("kind") == kind:
            groups.setdefault(int(s["window_start_ms"]), []).append(s)
    if cur_start and cur_used is not None and cur_cost is not None:
        groups.setdefault(int(cur_start), []).append(
            {"ts": now_ms(), "used_percent": float(cur_used), "cost": float(cur_cost)})

    tot_dc = tot_dp = 0.0
    segs = 0
    per_window: list[float] = []
    for samples in groups.values():
        samples.sort(key=lambda s: s["ts"])
        w_dc = w_dp = 0.0
        for a, b in zip(samples, samples[1:]):
            dp = b["used_percent"] - a["used_percent"]
            dc = b["cost"] - a["cost"]
            if dp >= 2 and dc > 0:          # 排除窗口重置(dp<0)与零成本空转
                w_dc += dc
                w_dp += dp
                segs += 1
        if w_dp > 0:
            tot_dc += w_dc
            tot_dp += w_dp
            per_window.append(w_dc / w_dp)
    if tot_dp <= 0:
        return {"samples": 0}

    per_pct = tot_dc / tot_dp
    spread = 0.0
    if len(per_window) > 1:
        mean = sum(per_window) / len(per_window)
        spread = (max(per_window) - min(per_window)) / mean * 100 if mean else 0.0
    return {"samples": segs, "per_percent": round(per_pct, 4),
            "capacity": round(per_pct * 100, 2), "spread_pct": round(spread, 1),
            "windows": len(per_window), "window_estimates": [round(v, 4) for v in per_window]}


def build_report(log: bool = True) -> dict:
    con = connect_ro(CPAMP_DB)
    prices = load_prices(con)
    history = load_state()
    out: dict = {"now_ms": now_ms(), "claude": {}, "codex": [], "other": {}}
    new_samples: list[dict] = []

    cq = claude_quota()
    claude = {"account": cq.get("account"), "error": cq.get("error"),
              "five_hour": cq.get("five_hour") or {}, "weekly": cq.get("weekly") or {},
              "extra_usage": cq.get("extra_usage"), "limits": cq.get("limits")}
    for key, uk, kind in (("five_hour", "five_hour_usage", "five_hour"),
                          ("weekly", "weekly_usage", "weekly")):
        w = claude.get(key) or {}
        if not w.get("reset_at_ms"):
            continue
        start = w["reset_at_ms"] - SPAN_MS[kind]
        w["window_start_ms"] = start
        uw = usage_window(con, prices, "claude", start, w["reset_at_ms"])
        claude[uk] = uw
        w["capacity"] = capacity_from_history(history, "claude", kind, start,
                                              w.get("used_percent"), uw.get("cost"))
        s = append_sample("claude", kind, start, w.get("used_percent"), uw.get("cost"), uw.get("calls"))
        if s and log:
            new_samples.append(s)
    out["claude"] = claude

    for q in codex_quota(con):
        entry = dict(q)
        if q.get("start_ms"):
            entry["usage"] = usage_window(con, prices, "codex", q["start_ms"], q.get("end_ms"))
            entry["capacity"] = capacity_from_history(
                history, "codex", q["window_kind"], q["start_ms"],
                q.get("used_percent"), entry["usage"].get("cost"))
            s = append_sample("codex", q["window_kind"], q["start_ms"], q.get("used_percent"),
                              entry["usage"].get("cost"), entry["usage"].get("calls"))
            if s and log:
                new_samples.append(s)
        out["codex"].append(entry)

    wk = claude.get("weekly") or {}
    if wk.get("reset_at_ms"):
        start = wk["reset_at_ms"] - SPAN_MS["weekly"]
        for prov in ("vertex",):
            out["other"][prov] = usage_window(con, prices, prov, start, wk["reset_at_ms"])

    con.close()
    if log and new_samples:
        append_state(new_samples)
    return out


def _cap_line(cap: dict, used, cur_cost) -> str:
    if not cap or not cap.get("samples"):
        if used:
            return "    反推:数据不足(单个样本),本窗口 1%% ≈ %s" % fmt_usd((cur_cost or 0) / used)
        return ""
    line = "    反推:100%% ≈ %s 等效(基于 %d 段样本 / %d 个窗口,离散 ±%.0f%%)" % (
        fmt_usd(cap["capacity"]), cap["samples"], cap["windows"], cap["spread_pct"])
    return line


def render(r: dict) -> str:
    L = ["CPA 用量速览  %s" % fmt_cst(r["now_ms"])]
    c = r.get("claude") or {}
    L.append("")
    L.append("【Claude 订阅】%s" % (c.get("account") or "?"))
    if c.get("error"):
        L.append("  额度读取失败: %s" % c["error"])
    for label, key, uk in (("5 小时", "five_hour", "five_hour_usage"), ("周", "weekly", "weekly_usage")):
        w = c.get(key) or {}
        u = w.get("used_percent")
        if u is None:
            continue
        L.append("  %s窗口  已用 %.0f%%  剩 %.0f%%    重置 %s(还有 %s)" % (
            label, u, 100 - u, fmt_cst(w.get("reset_at_ms")), left_str(w.get("reset_at_ms"), r["now_ms"])))
        uw = c.get(uk) or {}
        if uw.get("calls"):
            L.append("    本窗口 %s 请求 / %s token(未缓存入 %s、出 %s、缓读 %s、缓写 %s)/ 约 %s" % (
                uw["calls"], fmt_tok(uw["total"]), fmt_tok(uw["in"]), fmt_tok(uw["out"]),
                fmt_tok(uw["cache_read"]), fmt_tok(uw["cache_creation"]), fmt_usd(uw["cost"])))
            line = _cap_line(w.get("capacity") or {}, u, uw["cost"])
            if line:
                L.append(line)
                cap = (w["capacity"] or {}).get("capacity") or (uw["cost"] / (u / 100.0) if u else 0)
                if cap:
                    L.append("    推算剩余:剩 %.0f%% ≈ %s 等效" % (100 - u, fmt_usd(cap * (100 - u) / 100.0)))
    eu = c.get("extra_usage") or {}
    if eu.get("is_enabled") is False:
        L.append("  额外用量包:未启用(%s)" % (eu.get("disabled_reason") or "-"))

    if r.get("codex"):
        L.append("")
        L.append("【Codex】")
    for cd in r.get("codex") or []:
        u = cd.get("used_percent")
        L.append("  %s窗口  已用 %s%%  剩 %s%%    重置 %s(还有 %s)   [%s]" % (
            "5 小时" if cd["window_kind"] == "five_hour" else "周",
            "%.0f" % u if u is not None else "?", "%.0f" % (100 - u) if u is not None else "?",
            fmt_cst(cd.get("end_ms")), left_str(cd.get("end_ms"), r["now_ms"]), cd.get("source") or "-"))
        uw = cd.get("usage") or {}
        if uw.get("calls"):
            L.append("    本窗口 %s 请求 / %s token(未缓存入 %s、出 %s、缓读 %s)/ 约 %s   采样 %s" % (
                uw["calls"], fmt_tok(uw["total"]), fmt_tok(uw["in"]), fmt_tok(uw["out"]),
                fmt_tok(uw["cache_read"]), fmt_usd(uw["cost"]), fmt_cst(cd.get("observed_at_ms"))))
            line = _cap_line(cd.get("capacity") or {}, u, uw["cost"])
            if line:
                L.append(line)
        L.append("    账号 %s" % (cd.get("account") or "")[:78])

    if r.get("other"):
        for prov, uw in r["other"].items():
            if uw.get("calls"):
                L.append("")
                L.append("【其他】%s 近 7 天:%s 请求 / %s token / 约 %s(不走订阅额度)" % (
                    prov, uw["calls"], fmt_tok(uw["total"]), fmt_usd(uw["cost"])))
    L.append("")
    L.append("额度百分比来自上游(Claude OAuth 接口 / Codex 响应头);token 与费用来自 CPA 逐请求记录 × 官方价格表。")
    return "\n".join(L)


def main() -> int:
    r = build_report(log="--no-log" not in sys.argv)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str) if "--json" in sys.argv else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
