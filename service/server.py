#!/usr/bin/env python3
"""CPA 用量卡片服务 —— 只读,复用 cpa_usage.py 的数据层。

提供:
  GET  /usage/                     卡片式 UI(同源,可复用 CPAMP 面板登录态)
  GET  /usage/api/accounts         所有账号的卡片数据
  GET  /usage/api/accounts/<id>    单账号完整分析(详情页)
  GET  /usage/api/health           存活探针(免鉴权)

鉴权:请求头 Authorization: Bearer <CPAMP_ADMIN_KEY>,由 CPAMP 代为校验。
不改动 CPA / CPAMP 任何状态,只读 SQLite(file:...?mode=ro)+ Anthropic OAuth 用量接口。
"""
from __future__ import annotations

import hashlib
import glob
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cpa_usage as U  # noqa: E402


def _base_url(name, default):
    """Read a base URL and drop any path, so a stray API prefix cannot leak in."""
    value = os.environ.get(name, default).strip().rstrip("/")
    parts = urlsplit(value)
    if parts.path and parts.path != "/":
        value = "{}://{}".format(parts.scheme, parts.netloc)
    return value


HTML_PATH = os.path.join(HERE, "web", "index.html")
AUTHS_GLOB = os.environ.get("CPA_AUTHS_GLOB", "/root/CLIProxyAPI/auths/*.json")
CPAMP = _base_url("CPAMP_BASE_URL", "http://127.0.0.1:18317")
AUTH_PROBE = "/v0/management/usage-statistics-enabled"
HOST, PORT = os.environ.get("CPA_USAGE_BIND", "127.0.0.1"), int(os.environ.get("CPA_USAGE_PORT", "18390"))
CACHE_TTL = 30.0
KEY_TTL = 600.0

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}
_key_lock = threading.Lock()
_valid_keys: dict[str, float] = {}


# ---------------------------------------------------------------- 鉴权

def key_ok(key: str) -> bool:
    if not key:
        return False
    digest = hashlib.sha256(key.encode()).hexdigest()
    now = time.time()
    with _key_lock:
        exp = _valid_keys.get(digest)
        if exp and exp > now:
            return True
    req = urllib.request.Request(
        CPAMP + AUTH_PROBE, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            ok = 200 <= resp.status < 300
    except urllib.error.HTTPError:
        ok = False
    except Exception:  # noqa: BLE001
        ok = False          # CPAMP 不可达时不放行,避免鉴权被绕过
    if ok:
        with _key_lock:
            _valid_keys[digest] = now + KEY_TTL
            if len(_valid_keys) > 64:
                for k in [k for k, v in _valid_keys.items() if v < now]:
                    _valid_keys.pop(k, None)
    return ok


# ---------------------------------------------------------------- 数据加工

def _metered_label(prov: str) -> str:
    """按量计费账号显示名:从 auth 文件里找 email / 服务账号(client_email)。"""
    try:
        for path in sorted(glob.glob(AUTHS_GLOB)):
            name = os.path.basename(path).lower()
            if not name.startswith(prov[:6]):
                continue
            with open(path) as fh:
                d = json.load(fh)
            for field in ("email", "client_email", "account", "project_id"):
                val = d.get(field)
                if val and isinstance(val, str) and "@" in val:
                    return val
    except (OSError, json.JSONDecodeError):
        pass
    return prov


def _clean_org(org: str | None) -> str | None:
    """Claude 的 organization_name 形如 `foo@bar's Organization`,取干净部分。"""
    if not org:
        return None
    org = org.strip()
    for suffix in ("'s Organization", "'s Org", " Organization"):
        if org.endswith(suffix):
            org = org[: -len(suffix)]
    return org or None


def window_models(con, prices, provider, start_ms, end_ms=None, limit=12) -> list[dict]:
    """窗口内按模型拆分的请求数 / 分类 token / 按官方价格的费用。"""
    sql = ("select model, count(*),"
           " coalesce(sum(normalized_uncached_input_tokens),0),"
           " coalesce(sum(normalized_cache_read_tokens),0),"
           " coalesce(sum(cache_creation_tokens),0),"
           " coalesce(sum(output_tokens),0),"
           " coalesce(sum(total_tokens),0),"
           " coalesce(sum(case when failed=1 then 1 else 0 end),0)"
           " from usage_events where provider=? and timestamp_ms>=?")
    args: list = [provider, start_ms]
    if end_ms:
        sql += " and timestamp_ms<?"
        args.append(end_ms)
    sql += " group by model order by count(*) desc"
    out = []
    for model, calls, tin, cr, cc, tout, total, failed in con.execute(sql, args):
        pr = U.price_of(prices, model)
        cost = 0.0
        if pr:
            cost = ((tin / 1e6) * pr["in"] + (cr / 1e6) * pr["cache_read"]
                    + (cc / 1e6) * pr["cache_creation"] + (tout / 1e6) * pr["out"])
        out.append({"model": model, "calls": calls, "failed": failed,
                    "in": tin, "cache_read": cr, "cache_creation": cc, "out": tout,
                    "total": total, "priced": bool(pr), "cost": round(cost, 4)})
    out.sort(key=lambda x: -x["cost"])
    return out[:limit]


def full_windows(history: list[dict], provider: str, kind: str) -> list[dict]:
    """历史里跑到 ≥99% 的窗口,它们的实际花费就是「整窗容量」的实测值。"""
    groups: dict[int, list[dict]] = {}
    for s in history:
        if s.get("provider") == provider and s.get("kind") == kind:
            groups.setdefault(int(s.get("window_start_ms") or 0), []).append(s)
    out = []
    for start, samples in groups.items():
        if not start:
            continue
        samples.sort(key=lambda s: s.get("ts", 0))
        peak = max((s.get("used_percent") or 0) for s in samples)
        if peak >= 99:
            cost = max((s.get("cost") or 0) for s in samples)
            out.append({"window_start_ms": start, "peak_percent": round(peak, 1),
                        "cost": round(cost, 2)})
    out.sort(key=lambda x: -x["window_start_ms"])
    return out[:6]


def forecast(win: dict, usage: dict, est_total: float | None, frozen) -> dict:
    """面板式额度预测(搬 CPAMP 凭证页那套):预计请求 / 预计 Token / 预计花费 / 成功率。"""
    used = win.get("used_percent")
    calls = usage.get("calls") or 0
    failed = usage.get("failed") or 0
    total_tok = usage.get("total") or 0
    fc: dict = {"basis": None, "available": False}
    if used and used > 0:
        factor = 100.0 / used
        fc.update({
            "basis": "provider_percent",
            "available": True,
            "factor": round(factor, 3),
            "est_calls": int(round(calls * factor)),
            "est_tokens": int(round(total_tok * factor)),
            "est_cost": round((est_total if est_total is not None
                               else (usage.get("cost") or 0.0) * factor), 2),
            "success_rate": round((calls - failed) / calls * 100, 1) if calls else None,
            "est_success_calls": int(round((calls - failed) * factor)),
            "used_percent_snapshot": used,
        })
    elif frozen:
        fc.update({
            "basis": "frozen_window",
            "available": True,
            "est_calls": frozen.get("calls"),
            "est_tokens": frozen.get("total"),
            "est_cost": frozen.get("cost"),
            "success_rate": None,
            "source_window_start_ms": frozen.get("window_start_ms"),
        })
    return fc


def shape_window(con, prices, history, provider, kind, label, win, usage,
                 reset_ms, end_ms, source, observed_ms, cur_start) -> dict:
    used = win.get("used_percent")
    remaining = None if used is None else round(max(0.0, 100.0 - used), 1)
    cap_block = win.get("capacity") or {}
    cap = cap_block.get("capacity")

    est_total = est_basis = None
    if cap:
        est_total, est_basis = cap, "history"
    elif used and used > 0 and (usage or {}).get("cost"):
        est_total, est_basis = round(usage["cost"] * 100.0 / used, 2), "this_window"

    fw = full_windows(history, provider, kind)
    frozen = fw[0] if fw else None

    out = {
        "kind": kind, "label": label,
        "used_percent": used, "remaining_percent": remaining,
        "source": source, "observed_at_ms": observed_ms,
        "reset_at_ms": reset_ms, "end_ms": end_ms, "window_start_ms": cur_start,
        "observed_str": U.fmt_cst(observed_ms) if observed_ms else None,
        "reset_str": U.fmt_cst(reset_ms) if reset_ms else None,
        "left_str": U.left_str(reset_ms, U.now_ms()) if reset_ms else None,
        "span_ms": U.SPAN_MS.get(kind),
        "usage": usage or {},
        "capacity": cap_block,
        "est_total_cost": est_total, "est_basis": est_basis,
        "est_remaining_cost": (round(est_total * (remaining or 0) / 100.0, 2)
                               if est_total is not None else None),
        "per_percent_cost": (round(est_total / 100.0, 4) if est_total else None),
        "full_windows": fw,
    }
    out["forecast"] = forecast(win, usage or {}, est_total, frozen)
    return out


def _claude_auth_info() -> dict:
    """只读 Claude auth 文件的非敏感字段,不碰 token。"""
    try:
        for path in sorted(glob.glob(U.CLAUDE_AUTH_GLOB)):
            with open(path) as fh:
                d = json.load(fh)
            return {"org": d.get("organization_name"), "expired": d.get("expired"),
                    "disabled": d.get("disabled")}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def parse_account_key(key: str) -> dict:
    """CPAMP 的 account_key 形如 `usage-account-history:<v>:<scope>:<hex 段>...`,
    同一账号会以 codex-account / codex-member 两种 scope 出现,按 uuid 归并成一张卡。"""
    parts = (key or "").split(":")
    uuid = email = None
    for seg in parts[2:]:
        if not seg or seg == "***":
            continue
        if U.HEXSEG.match(seg):
            try:
                seg = bytes.fromhex(seg).decode("utf-8", "ignore")
            except ValueError:
                continue
        if "@" in seg:
            email = seg
        elif len(seg) == 36 and seg.count("-") == 4:
            uuid = seg
    return {"uuid": uuid, "email": email, "scope": parts[2] if len(parts) > 2 else "",
            "identity": uuid or email or key,
            "label": email or (uuid[:8] if uuid else (key or "?")[:14])}


def _sample(report: dict) -> None:
    """记录采样:同一窗口内数值没变化就不重复写,避免把 state.jsonl 刷爆。"""
    cand: list[dict] = []
    c = report.get("claude") or {}
    for key, uk, kind in (("five_hour", "five_hour_usage", "five_hour"),
                          ("weekly", "weekly_usage", "weekly")):
        w, uw = c.get(key) or {}, c.get(uk) or {}
        s = U.append_sample("claude", kind, w.get("window_start_ms"),
                            w.get("used_percent"), uw.get("cost"), uw.get("calls"))
        if s:
            cand.append(s)
    for e in report.get("codex") or []:
        uw = e.get("usage") or {}
        s = U.append_sample("codex", e.get("window_kind"), e.get("start_ms"),
                            e.get("used_percent"), uw.get("cost"), uw.get("calls"))
        if s:
            cand.append(s)
    if not cand:
        return
    last: dict[tuple, dict] = {}
    for s in U.load_state():
        last[(s.get("provider"), s.get("kind"), s.get("window_start_ms"))] = s
    now, keep = U.now_ms(), []
    for s in cand:
        k = (s["provider"], s["kind"], s["window_start_ms"])
        p = last.get(k)
        if (p is None or p.get("used_percent") != s["used_percent"]
                or abs((p.get("cost") or 0) - (s["cost"] or 0)) > 0.01
                or now - (p.get("ts") or 0) > 10 * 60 * 1000):
            keep.append(s)
            last[k] = s
    if keep:
        U.append_state(keep)


def collect(force: bool = False) -> dict:
    with _cache_lock:
        if (not force and _cache["data"] is not None
                and time.time() - _cache["ts"] < CACHE_TTL):
            return _cache["data"]

    report = U.build_report(log=False)
    _sample(report)
    con = U.connect_ro(U.CPAMP_DB)
    prices = U.load_prices(con)
    history = U.load_state()
    accounts: list[dict] = []

    # ---- Claude(额度走 Anthropic OAuth 用量接口,实时)
    c = report.get("claude") or {}
    c_windows = []
    for key, uk, kind, label in (("five_hour", "five_hour_usage", "five_hour", "5 小时"),
                                 ("weekly", "weekly_usage", "weekly", "周")):
        w = c.get(key) or {}
        if w.get("used_percent") is None:
            continue
        usage = c.get(uk) or {}
        start = w.get("window_start_ms")
        block = shape_window(con, prices, history, "claude", kind, label, w, usage,
                             w.get("reset_at_ms"), w.get("reset_at_ms"),
                             "oauth_live", U.now_ms(), start)
        block["models"] = window_models(con, prices, "claude", start,
                                        w.get("reset_at_ms")) if start else []
        c_windows.append(block)
    c_acct = c.get("account")
    c_plan = _clean_org(_claude_auth_info().get("org"))
    if c_plan and c_acct and c_plan.lower() == c_acct.lower():
        c_plan = None
    accounts.append({
        "id": "claude", "provider": "claude", "provider_label": "Claude",
        "account": c_acct, "plan": c_plan,
        "kind": "subscription",
        "windows": c_windows, "error": c.get("error"),
        "extra_usage": c.get("extra_usage"), "limits": c.get("limits") or [],
        "note": "额度来自 Anthropic OAuth 用量接口(每次加载实时拉取)",
        "note_key": "claude_oauth",
    })

    # ---- Codex(额度随响应头被动刷新,可能滞后于最后一次请求)
    merged: dict[str, dict] = {}
    for entry in report.get("codex") or []:
        info = parse_account_key(entry.get("account_key") or "")
        slot = merged.setdefault(info["identity"], {"info": info, "wins": {}})
        kind = entry["window_kind"]
        prev = slot["wins"].get(kind)
        if prev is None or (entry.get("observed_at_ms") or 0) > (prev.get("observed_at_ms") or 0):
            slot["wins"][kind] = entry
    for identity, slot in merged.items():
        info = slot["info"]
        entries = list(slot["wins"].values())
        windows = []
        for e in entries:
            kind = e["window_kind"]
            label = "5 小时" if kind == "five_hour" else "周"
            usage = e.get("usage") or {}
            win = {"used_percent": e.get("used_percent"), "capacity": e.get("capacity")}
            block = shape_window(con, prices, history, "codex", kind, label, win, usage,
                                 e.get("end_ms"), e.get("end_ms"), e.get("source"),
                                 e.get("observed_at_ms"), e.get("start_ms"))
            block["models"] = window_models(con, prices, "codex", e["start_ms"],
                                            e.get("end_ms")) if e.get("start_ms") else []
            windows.append(block)
        windows.sort(key=lambda w: w["kind"] != "five_hour")
        slug = (info["uuid"] or info["email"] or identity)[:8]
        accounts.append({
            "id": "codex-" + slug, "provider": "codex",
            "provider_label": "Codex", "account": info["label"],
            "account_raw": next((e.get("account") for e in entries), None),
            "account_scope": info["scope"],
            "plan": next((e.get("plan") for e in entries if e.get("plan")), None),
            "kind": "subscription", "windows": windows, "error": None,
            "note": "额度百分比随每个请求的响应头上报,数字可能滞后于最后一次请求",
            "note_key": "codex_header",
        })

    # ---- 其他(不走订阅额度,只有花费)
    for prov, usage in (report.get("other") or {}).items():
        wk = (report.get("claude") or {}).get("weekly") or {}
        start = wk.get("window_start_ms")
        if not start:
            continue
        accounts.append({
            "id": prov, "provider": prov, "provider_label": prov.capitalize(),
            "account": _metered_label(prov), "plan": None, "kind": "metered",
            "windows": [], "error": None,
            "spend": usage,
            "spend_models": window_models(con, prices, prov, start, wk.get("reset_at_ms")),
            "note": "按量计费账号,没有订阅额度窗口;金额为近 7 天实际消耗",
            "note_key": "metered",
        })
    con.close()

    data = {"generated_ms": U.now_ms(), "generated_str": U.fmt_cst(U.now_ms()),
            "accounts": accounts}
    with _cache_lock:
        _cache.update({"ts": time.time(), "data": data})
    return data


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "cpa-usage"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def _auth(self) -> bool:
        hdr = self.headers.get("Authorization") or ""
        key = hdr[7:].strip() if hdr.lower().startswith("bearer ") else ""
        if not key:
            key = self.headers.get("X-Cpa-Usage-Key") or ""
        if key_ok(key):
            return True
        self._json(401, {"error": "unauthorized", "hint": "需要 CPAMP 管理密钥"})
        return False

    def _redirect(self, to: str):
        self.send_response(301)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        raw = u.path
        path = raw.rstrip("/") or "/"
        qs = parse_qs(u.query)

        if path in ("/usage/api/health", "/api/health"):
            return self._json(200, {"ok": True, "ts": int(time.time() * 1000)})

        if raw in ("/usage/", "/usage/index.html"):
            try:
                with open(HTML_PATH, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError as exc:
                return self._send(500, ("UI 文件缺失: %s" % exc).encode(),
                                  "text/plain; charset=utf-8")

        if path in ("/usage", ""):
            return self._redirect("/usage/")

        if path in ("/usage/api/accounts", "/api/accounts"):
            if not self._auth():
                return
            try:
                data = collect(force="force" in qs)
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return self._json(200, data)

        if path.startswith(("/usage/api/accounts/", "/api/accounts/")):
            if not self._auth():
                return
            want = unquote(path.rsplit("/", 1)[-1])
            try:
                data = collect(force="force" in qs)
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            for acct in data["accounts"]:
                if acct["id"] == want:
                    return self._json(200, {"generated_ms": data["generated_ms"],
                                            "generated_str": data["generated_str"],
                                            "account": acct})
            return self._json(404, {"error": "account not found", "id": want})

        return self._json(404, {"error": "not found", "path": path})


def main() -> int:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("cpa-usage 卡片服务 http://%s:%d (UI: /usage/)" % (HOST, PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
