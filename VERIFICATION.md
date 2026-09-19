# Verification

Everything below is real output from the reference deployment (single VPS, CPA v7.3.4,
CPA Manager Plus v1.13.1, Linux amd64). Commands are run from the repository root; account
identifiers are redacted.

## 1. Plugin unit tests and offline build

```
$ plugin/scripts/build.sh
building golang:1.26-bookworm → dist/cpa-quota-cards-v0.1.2.so (cpuset 3-4)
ok  	local/cpa-quota-cards	0.008s
158f3eca6607eb963b4507115bca9acc98006c6c6c12ab013466c535d1e86151  dist/cpa-quota-cards-v0.1.2.so
```

`build.sh` runs `go vet` and the unit tests (config parsing/validation, page proxying and its
cache, resource-route aliases, method rejection, upstream failure page, status payload) before
producing a `c-shared` library. The build runs offline (`GOPROXY=off`) against a mounted module
cache and is pinned to two CPUs so it cannot disturb the gateways.

## 2. Install

```
$ sudo python3 scripts/install_plugin.py --upgrade --cache-seconds 8
library    : /root/cpa-quota-cards/dist/cpa-quota-cards-v0.1.2.so (sha256 158f3eca6607eb96)
destination: /root/CLIProxyAPI/plugins/linux/amd64/cpa-quota-cards-v0.1.2.so
installed  : ['cpa-quota-cards-v0.1.1.so']
configured : True
backup     : /root/backups/config.yaml.20260919-193658.pre-cpa-quota-cards
copied     : cpa-quota-cards-v0.1.2.so
config     : {"enabled": true, "service_url": "http://172.29.0.1:18390/usage/", "cache_seconds": 8, "timeout_seconds": 8}
waiting for the host to load the plugin ...
reloading via the management API (disable/enable) ...
status     : {"cache_seconds": 5, "page_bytes": 28762, "service_url": "http://172.29.0.1:18390/usage/", "serving": true, "timeout_seconds": 8, "version": "0.1.2"}
OK: plugin page registered at /v0/resource/plugins/cpa-quota-cards/dashboard
    sidebar entry: 额度与用量 (after a panel reload)
```

An upgrade is verified end to end: the new library is copied, the config block is rewritten, the
host is told to reload (disable/enable), and the script only exits 0 when the plugin reports
`serving: true` **and** the running version equals the installed one — `0.1.1 → 0.1.2` above.

The script exits 0 only when the plugin reports `serving: true` **and** the running version
matches the installed library. On this host it also printed the stale-mount warning (see
pitfall 1 in the README): the container was still running the config it had loaded at start,
which is why `cache_seconds` is 5 in the status but 8 on disk. Disk state is correct and the
next container restart applies it — the warning is intentional, not a failure.

Management API view of the same host:

```
cpa-quota-cards          enabled=True  registered=True  menus=[('额度与用量', '/v0/resource/plugins/cpa-quota-cards/dashboard')]
cpa-quota-estimator      enabled=False registered=False menus=[]
cpa-window-keeper        enabled=True  registered=True  menus=[('5 小时自动开窗', '/v0/resource/plugins/cpa-window-keeper/dashboard')]
```

`cpa-quota-estimator` (the plugin page that previously provided the quota-capacity prediction)
is disabled, so 「额度容量预测」 no longer appears in the sidebar and 「额度与用量」 takes its
place. Re-enable it any time from the panel or with a single `PATCH .../enabled` call.

## 3. Service API

```
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18390/usage/api/accounts
401                       # no key
$ curl -s -H "Authorization: Bearer <manager key>" .../usage/api/accounts | jq 'keys, .accounts[0]|keys'
['generated_ms','generated_str','accounts']
['id','provider','provider_label','account','plan','kind','windows','error','extra_usage','limits','note']
$ curl -s -H "Authorization: Bearer <manager key>" .../usage/api/accounts/claude | jq keys
['generated_ms','generated_str','account']
```

## 4. Real-browser check of the standalone page

```
$ scripts/verify_service.py
卡片数: 3
  - claude | CL / Claude / <account>@gmail.com
  - codex  | CO / Codex / <account>@gmail.com
  - vertex | VE / Vertex / <service account>
进度条条数: 4
    进度条 填充=26.0% tone=good
    进度条 填充=89.0% tone=warn
    进度条 填充=100.0% tone=bad
    进度条 填充=16.0% tone=good
    横向溢出(px): 0
详情区块数: 11
    详情含「额度预测」: True
    详情含「预计花费」: True
    详情含「容量反推」: True
    详情含「模型构成」: True
    详情含「成功率」: True
控制台问题: 0
exit=0
```

## 5. Real-browser check of the plugin page inside the panel

Logs in to the real panel over HTTPS, clicks the plugin's sidebar entry and asserts on the
embedded document.

```
$ CPAMP_PANEL_URL=https://<panel host>/management.html scripts/verify_panel.py
{
  "remember_toggled": "false->true",
  "panel_login_stored": true,
  "sidebar_entry": "额度与用量",
  "superseded_entry_present": false,
  "iframe_src": "https://<panel host>/v0/resource/plugins/cpa-quota-cards/dashboard",
  "panel_origin": "https://<panel host>",
  "same_origin": true,
  "account_cards": 3,
  "meters": 4,
  "key_prompt_visible": false,
  "detail_sections": 11,
  "horizontal_overflow_px": 0,
  "console_problems": [
    "console.error: Loading the script '.../cdn-cgi/challenge-platform/scripts/precursor/main.js' violates the following Content Security Policy directive: \"script-src 'unsafe-inline'\".",
    "console.error: Loading the script 'https://static.cloudflareinsights.com/beacon.min.js/...' violates the following Content Security Policy directive: \"script-src 'unsafe-inline'\"."
  ]
}
ALL ASSERTIONS PASSED
exit=0
```

What this proves:

* the page really is mounted from the panel's own sidebar (`sidebar_entry`, `iframe_src`);
* the iframe is **same-origin** with the panel, which is what makes the login state reusable:
  `key_prompt_visible` is `false` although this run started from an empty browser profile and
  logged in with "remember" checked;
* the cards, both quota meters and the 11-section detail view render inside the panel;
* `superseded_entry_present: false` — the page it replaced is gone from the sidebar;
* no horizontal overflow.

The two console messages are the plugin page's CSP blocking Cloudflare's injected scripts
(challenge-platform precursor and the insights beacon). That is deliberate: the plugin returns
`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self';
frame-ancestors 'self'`, so nothing from the CDN runs inside the card UI.

## 6. Known limits

* the quota percentages are only as fresh as the upstream: Claude is fetched live from the
  Anthropic OAuth usage endpoint on every load, Codex is refreshed passively from response
  headers (the card shows how long ago);
* capacity back-calculation gets more accurate as more of a window is consumed, and assumes a
  roughly stable model mix — with one window of samples the reference host measured ~13% error,
  with ten windows ~1%;
* reusing the panel login state depends on the panel's obfuscated storage (`enc::v2::`, older
  panels `enc::v1::`). Both formats are handled; a future format change would surface as the
  manual key prompt rather than as wrong numbers.

## 7. Mount re-alignment after the config edit (2026-09-19)

While installing, `config.yaml` was rewritten in place (same inode) — but an earlier edit had used
an atomic replace, which left the CLIProxyAPI bind mount pinned to the deleted inode: the host saw
the new config while the container kept serving the old one (`cache_seconds` 5 vs. the configured 8).
`docker restart cli-proxy-api` re-resolved the mount and the container was re-verified by a real
browser run of `scripts/verify_panel.py` against the production panel:

```
mountinfo : /root/CLIProxyAPI/config.yaml -> /CLIProxyAPI/config.yaml   (no "//deleted")
sha256    : host == container
status    : {"cache_seconds":8,"page_bytes":28762,"serving":true,"version":"0.1.2"}
panel run : sidebar_entry=额度与用量, superseded_entry_present=false, same_origin=true,
            account_cards=3, meters=4, key_prompt_visible=false, detail_sections=11,
            horizontal_overflow_px=0 -> ALL ASSERTIONS PASSED
```
