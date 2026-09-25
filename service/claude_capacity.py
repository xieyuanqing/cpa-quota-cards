#!/usr/bin/env python3
"""Claude per-request capacity back-calculation.

Every Claude response carries Anthropic-Ratelimit-Unified-{5h,7d}-Utilization
headers, and CPAMP keeps them in usage_events.raw_json. Replaying requests in
order gives, at each request:

    cpa_cost   = CPA spend since the window started (official prices)
    used_pct   = utilization the upstream reports right after this request
    cum_est    = cpa_cost / used_pct * 100   ("if all usage were CPA, 100% = $X")

and, whenever the percentage steps up, a local estimate over the most recent
steps (step_est = delta cost / delta percent). If only CPA is using the
account, both stay roughly flat. A sudden drop of step_est means the
percentage rose faster than CPA spend explains -> something else (the app)
used the quota in that stretch.

Read-only: opens the CPAMP database with mode=ro and writes nothing.
"""
from __future__ import annotations

import json
import sys

import cpa_usage as U

KINDS = {"weekly": ("7d", 7 * 24 * 3600 * 1000), "five_hour": ("5h", 5 * 3600 * 1000)}
STEP_SPAN = 3  # percentage points each local estimate spans (utilization is 1%-quantized)


def _hdr(headers: dict, name: str):
    for k, v in headers.items():
        if k.lower() == name:
            return v[0] if isinstance(v, list) and v else v
    return None


def request_cost(prices, model, tin, cr, cc, tout) -> float:
    pr = U.price_of(prices, model)
    if not pr:
        return 0.0
    return ((tin or 0) / 1e6 * pr["in"] + (cr or 0) / 1e6 * pr["cache_read"]
            + (cc or 0) / 1e6 * pr["cache_creation"] + (tout or 0) / 1e6 * pr["out"])


def load_requests(con, prices, since_ms: int = 0) -> list[dict]:
    """All Claude requests with their cost and quota headers, oldest first."""
    out = []
    sql = ("select timestamp_ms, model, normalized_uncached_input_tokens,"
           " normalized_cache_read_tokens, cache_creation_tokens, output_tokens,"
           " failed, raw_json from usage_events"
           " where provider='claude' and timestamp_ms>=? order by timestamp_ms, id")
    for ts, model, tin, cr, cc, tout, failed, raw in con.execute(sql, (since_ms,)):
        try:
            headers = (json.loads(raw or "{}").get("response_headers")) or {}
        except json.JSONDecodeError:
            headers = {}
        row = {"ts": ts, "model": model, "failed": bool(failed),
               "cost": request_cost(prices, model, tin, cr, cc, tout)}
        for kind, (tag, _span) in KINDS.items():
            util = _hdr(headers, "anthropic-ratelimit-unified-%s-utilization" % tag)
            reset = _hdr(headers, "anthropic-ratelimit-unified-%s-reset" % tag)
            try:
                row[kind] = (float(str(util)) * 100.0, int(str(reset)) * 1000) if util and reset else None
            except ValueError:
                row[kind] = None
        out.append(row)
    return out


def replay(requests: list[dict], kind: str) -> list[dict]:
    """Group by quota window and emit one point per request that reported quota."""
    span = KINDS[kind][1]
    windows: dict[int, dict] = {}
    # Attribute every request (even those without headers) to the window it
    # fell in, so cost is complete; windows are keyed by their reset time.
    known_resets = sorted({r[kind][1] for r in requests if r.get(kind)})

    def window_of(ts):
        for reset in known_resets:
            if reset - span <= ts < reset:
                return reset
        return None

    for r in requests:
        reset = window_of(r["ts"])
        if reset is None:
            continue
        w = windows.setdefault(reset, {"reset_ms": reset, "start_ms": reset - span,
                                       "cost": 0.0, "calls": 0, "points": [], "steps": []})
        w["cost"] += r["cost"]
        w["calls"] += 1
        q = r.get(kind)
        if not q or q[1] != reset:
            continue
        used = q[0]
        pt = {"ts": r["ts"], "used": round(used, 1), "cost": round(w["cost"], 4)}
        if used > 0:
            pt["cum_est"] = round(w["cost"] / used * 100.0, 2)
        steps = w["steps"]
        if not steps or used > steps[-1]["used"]:
            steps.append({"ts": r["ts"], "used": used, "cost": w["cost"]})
            # local estimate over the last STEP_SPAN percentage points
            base = None
            for s in reversed(steps[:-1]):
                if used - s["used"] >= STEP_SPAN:
                    base = s
                    break
            if base is not None:
                dp, dc = used - base["used"], w["cost"] - base["cost"]
                pt["step_est"] = round(dc / dp * 100.0, 2)
                pt["step_from"] = round(base["used"], 1)
                pt["step_hours"] = round((r["ts"] - base["ts"]) / 3600000, 2)
        w["points"].append(pt)
    out = []
    for reset in sorted(windows):
        w = windows[reset]
        w.pop("steps")
        w["cost"] = round(w["cost"], 2)
        last = w["points"][-1] if w["points"] else {}
        w["final_used"] = last.get("used")
        w["final_cum_est"] = last.get("cum_est")
        out.append(w)
    return out


CLEAN_MAX_HOURS = 1.5  # a step counts as "CPA only" if it spanned at most this long


def _median(vals: list[float]):
    vals = sorted(vals)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def summarize(win: dict, fallback: list[dict] | None = None) -> dict:
    """Split one window into a CPA share and an app/other share.

    Capacity = median of the "clean" step estimates (steps that covered
    STEP_SPAN points within CLEAN_MAX_HOURS, i.e. CPA was busy the whole
    time). Steps that took long are where the app most likely chipped in, so
    they are recorded but not trusted. With too few clean steps in this
    window, earlier windows' clean steps are borrowed.
    """
    clean = [p["step_est"] for p in win["points"]
             if "step_est" in p and p["step_hours"] <= CLEAN_MAX_HOURS and p["step_est"] > 0]
    basis = "this_window"
    pool = list(clean)
    if len(pool) < 3 and fallback:
        for w in reversed(fallback):
            pool += [p["step_est"] for p in w["points"]
                     if "step_est" in p and p["step_hours"] <= CLEAN_MAX_HOURS and p["step_est"] > 0]
        basis = "previous_windows"
    cap = _median(pool)
    used = win.get("final_used")
    out = {"capacity": round(cap, 2) if cap else None, "basis": basis if cap else None,
           "clean_steps": len(clean), "pool_steps": len(pool),
           "clean_min": round(min(pool), 2) if pool else None,
           "clean_max": round(max(pool), 2) if pool else None,
           "used": used, "cpa_cost": win["cost"], "cum_est": win.get("final_cum_est")}
    if cap and used is not None:
        cpa_pct = min(used, win["cost"] / cap * 100.0)
        out["cpa_pct"] = round(cpa_pct, 1)
        out["app_pct"] = round(max(0.0, used - cpa_pct), 1)
        out["remaining_cost"] = round(cap * max(0.0, 100.0 - used) / 100.0, 2)
    # compact trajectory for the UI: every percent step with its estimates
    out["trajectory"] = [
        {"ts": p["ts"], "used": p["used"], "cost": round(p["cost"], 2), "cum_est": p.get("cum_est"),
         "step_est": p.get("step_est"), "step_hours": p.get("step_hours"),
         "clean": ("step_est" in p and p["step_hours"] <= CLEAN_MAX_HOURS)}
        for p in win["points"] if "step_est" in p or p is win["points"][-1]]
    return out


def current_summary(kind: str, reset_ms: int | None) -> dict | None:
    """Summary for the window ending at reset_ms (tolerates a few seconds of jitter)."""
    wins = build(kind)
    if not wins:
        return None
    idx = None
    if reset_ms:
        for i, w in enumerate(wins):
            if abs(w["reset_ms"] - reset_ms) < 120_000:
                idx = i
    if idx is None:
        idx = len(wins) - 1
    s = summarize(wins[idx], wins[:idx])
    s["history"] = [{"start_ms": w["start_ms"], "reset_ms": w["reset_ms"], "cost": w["cost"],
                     "final_used": w["final_used"],
                     "capacity": summarize(w)["capacity"]} for w in wins[:idx]][-6:]
    return s


def build(kind: str = "weekly") -> list[dict]:
    con = U.connect_ro(U.CPAMP_DB)
    try:
        prices = U.load_prices(con)
        return replay(load_requests(con, prices), kind)
    finally:
        con.close()


def main() -> int:
    kind = "five_hour" if "--5h" in sys.argv else "weekly"
    wins = build(kind)
    if "--json" in sys.argv:
        print(json.dumps(wins, ensure_ascii=False))
        return 0
    for i, w in enumerate(wins):
        s = summarize(w, wins[:i])
        print("\n## 汇总: 容量 %s (依据 %s, 干净段 %d, 范围 %s~%s)  CPA 占 %s%%  APP 占 %s%%" % (
            s["capacity"], s["basis"], s["clean_steps"], s["clean_min"], s["clean_max"],
            s.get("cpa_pct"), s.get("app_pct")))
        print("== 窗口 %s -> %s  CPA %d 请求 $%.2f  最终 %s%%  累计反推 %s" % (
            U.fmt_cst(w["start_ms"]), U.fmt_cst(w["reset_ms"]), w["calls"], w["cost"],
            w["final_used"], w["final_cum_est"]))
        for p in w["points"]:
            if "step_est" in p:
                print("  %s  %5.1f%%  CPA $%7.2f  累计反推 $%7.2f  近%d点反推 $%7.2f (%.1f%%起, 用时 %.1fh)" % (
                    U.fmt_cst(p["ts"]), p["used"], p["cost"], p.get("cum_est") or 0,
                    STEP_SPAN, p["step_est"], p["step_from"], p["step_hours"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
