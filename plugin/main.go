// Command cpa-quota-cards is a native CLIProxyAPI plugin that embeds the
// cpa-quota-cards quota/usage dashboard into the manager sidebar as a plugin page.
//
// It owns no quota logic of its own: the page is served by the companion
// read-only HTTP service (see service/server.py, default http://127.0.0.1:18390/usage/)
// and this plugin only proxies those bytes into an authenticated management UI
// resource route, so the panel sidebar can open it same-origin.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	resourcePath   = "/dashboard"
	pageLimitBytes = 4 << 20
	// The page is inline-only (no external assets), so a tight policy works.
	pageCSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
		"connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'"
)

type App struct {
	mu     sync.Mutex
	cfg    Config
	cache  cacheEntry
	client *http.Client
	err    string
}

type cacheEntry struct {
	at   time.Time
	body []byte
	err  string
}

var global = &App{cfg: defaultConfig(), client: &http.Client{}}

func (a *App) configure(raw []byte) error {
	cfg, err := parseConfig(raw)
	if err != nil {
		a.mu.Lock()
		a.err = err.Error()
		a.mu.Unlock()
		return err
	}
	a.mu.Lock()
	a.cfg = cfg
	a.cache = cacheEntry{}
	a.err = ""
	a.mu.Unlock()
	return nil
}

// shutdown has nothing to join: this plugin starts no goroutines and makes no
// host callbacks, so there is no work that could touch a freed host pointer.
func (a *App) shutdown() {
	a.mu.Lock()
	a.cache = cacheEntry{}
	a.mu.Unlock()
}

func (a *App) handle(method string, raw []byte) ([]byte, error) {
	switch method {
	case "plugin.register", "plugin.reconfigure":
		var req struct {
			ConfigYAML []byte `json:"config_yaml"`
		}
		if json.Unmarshal(raw, &req) != nil {
			return nil, errors.New("invalid lifecycle request")
		}
		if err := a.configure(req.ConfigYAML); err != nil {
			return nil, err
		}
		return okEnvelope(registration())
	case "plugin.quiesce", "plugin.shutdown":
		a.shutdown()
		return okEnvelope(map[string]any{})
	case "management.register":
		return okEnvelope(map[string]any{
			"routes": []any{map[string]any{"Method": "GET", "Path": "/" + pluginID + "/status"}},
			"resources": []any{map[string]any{
				"Path":        resourcePath,
				"Menu":        "额度与用量",
				"Description": "按账号展示 5 小时 / 周额度使用百分比、本窗口与整窗金额，以及额度容量反推与预测；数据由本机只读用量服务提供。",
			}},
		})
	case "management.handle":
		return a.management(raw)
	default:
		return nil, errors.New("unsupported plugin method")
	}
}

func registration() any {
	field := func(name, kind, desc string) any {
		return map[string]any{"Name": name, "Type": kind, "Description": desc}
	}
	return map[string]any{"schema_version": 6, "metadata": map[string]any{
		"Name":             "CPA Quota Cards",
		"Version":          version,
		"Author":           "晴空 / 琴音",
		"GitHubRepository": "https://github.com/xieyuanqing/cpa-quota-cards",
		"ConfigFields": []any{
			field("service_url", "string", "用量服务页面地址，默认 http://127.0.0.1:18390/usage/（server.py 的 /usage/ 路径）"),
			field("cache_seconds", "integer", "页面字节缓存秒数，默认 5；0 表示每次请求都回源"),
			field("timeout_seconds", "integer", "回源超时秒数，默认 8，范围 1-60"),
		},
	}, "capabilities": map[string]any{"management_api": true}}
}

type managementRequest struct {
	Method, Path string
	Headers      http.Header
	Body         []byte
}

type managementResponse struct {
	StatusCode int
	Headers    http.Header
	Body       []byte
}

func response(status int, body []byte, contentType string) managementResponse {
	return managementResponse{StatusCode: status, Body: body, Headers: http.Header{
		"Content-Type":           {contentType},
		"Cache-Control":          {"no-store"},
		"X-Content-Type-Options": {"nosniff"},
		"Content-Security-Policy": {pageCSP},
	}}
}

func (a *App) management(raw []byte) ([]byte, error) {
	var req managementRequest
	if json.Unmarshal(raw, &req) != nil {
		return nil, errors.New("invalid management request")
	}
	path := strings.TrimSuffix(strings.TrimSpace(req.Path), "/")
	switch path {
	case resourcePath, "/v0/resource/plugins/" + pluginID + resourcePath:
		if req.Method != http.MethodGet {
			return okEnvelope(response(http.StatusMethodNotAllowed, []byte("method not allowed"), "text/plain; charset=utf-8"))
		}
		body, failure := a.page()
		if failure != "" {
			url := a.config().ServiceURL
			return okEnvelope(response(http.StatusOK, errorPage(failure, url), "text/html; charset=utf-8"))
		}
		return okEnvelope(response(http.StatusOK, body, "text/html; charset=utf-8"))
	case "/" + pluginID + "/status", "/v0/management/" + pluginID + "/status":
		if req.Method != http.MethodGet {
			return okEnvelope(response(http.StatusMethodNotAllowed, []byte("method not allowed"), "text/plain; charset=utf-8"))
		}
		return okEnvelope(response(http.StatusOK, a.status(), "application/json"))
	}
	return okEnvelope(response(http.StatusNotFound, []byte("not found"), "text/plain; charset=utf-8"))
}

func (a *App) status() []byte {
	a.mu.Lock()
	cfg, errMsg := a.cfg, a.err
	a.mu.Unlock()
	body, failure := a.pageFresh(cfg)
	out := map[string]any{
		"version":         version,
		"service_url":     cfg.ServiceURL,
		"cache_seconds":   cfg.CacheSeconds,
		"timeout_seconds": cfg.TimeoutSeconds,
		"page_bytes":      len(body),
		"serving":         failure == "",
	}
	if errMsg != "" {
		out["config_error"] = errMsg
	}
	if failure != "" {
		out["error"] = failure
	}
	raw, err := json.Marshal(out)
	if err != nil {
		return []byte(`{"serving":false,"error":"status encode failed"}`)
	}
	return raw
}

// config returns a snapshot of the effective configuration.
func (a *App) config() Config {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.cfg
}

// page returns the upstream page bytes, reusing a recently fetched copy.
func (a *App) page() ([]byte, string) {
	a.mu.Lock()
	cfg, cached := a.cfg, a.cache
	a.mu.Unlock()
	if cached.at.IsZero() {
		return a.pageFresh(cfg)
	}
	if time.Since(cached.at) < time.Duration(cfg.CacheSeconds)*time.Second {
		return cached.body, cached.err
	}
	return a.pageFresh(cfg)
}

func (a *App) pageFresh(cfg Config) ([]byte, string) {
	body, failure := a.fetch(cfg)
	a.mu.Lock()
	a.cache = cacheEntry{at: time.Now(), body: body, err: failure}
	a.mu.Unlock()
	return body, failure
}

func (a *App) fetch(cfg Config) ([]byte, string) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(cfg.TimeoutSeconds)*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, cfg.ServiceURL, nil)
	if err != nil {
		return nil, "invalid service_url: " + err.Error()
	}
	req.Header.Set("Accept", "text/html")
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, "service unreachable: " + err.Error()
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, pageLimitBytes))
	if err != nil {
		return nil, "service read failed: " + err.Error()
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Sprintf("service returned HTTP %d", resp.StatusCode)
	}
	if len(body) == 0 {
		return nil, "service returned an empty page"
	}
	return body, ""
}

func errorPage(failure, serviceURL string) []byte {
	var b strings.Builder
	b.WriteString(`<!doctype html><html lang="zh-CN"><meta charset="utf-8">`)
	b.WriteString(`<meta name="viewport" content="width=device-width,initial-scale=1"><title>额度与用量</title>`)
	b.WriteString(`<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;`)
	b.WriteString(`font:14px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--app-surface,#0f1115);`)
	b.WriteString(`color:var(--text-primary,#e6e8ee)}.card{max-width:520px;padding:22px 24px;border-radius:14px;`)
	b.WriteString(`border:1px solid var(--border-color,rgba(255,255,255,.14));background:rgba(255,255,255,.04)}`)
	b.WriteString(`h1{margin:0 0 8px;font-size:16px}code{font-size:12px;opacity:.85}p{margin:6px 0;opacity:.8}</style>`)
	b.WriteString(`<div class="card"><h1>额度服务暂时不可用</h1>`)
	b.WriteString(`<p>`)
	b.WriteString(html.EscapeString(failure))
	b.WriteString(`</p><p>请确认用量服务已启动：<code>systemctl status cpa-quota-cards</code></p>`)
	b.WriteString(`<p>页面直连地址：<code>`)
	b.WriteString(html.EscapeString(serviceURL))
	b.WriteString(`</code></p>`)
	b.WriteString(`</div></html>`)
	return []byte(b.String())
}
