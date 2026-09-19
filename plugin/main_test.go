package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestApp() *App {
	return &App{cfg: defaultConfig(), client: &http.Client{}}
}

// envelopeResult decodes the plugin's {ok,result} envelope into the management
// response shape the host expects.
func envelopeResult(t *testing.T, raw []byte) managementResponse {
	t.Helper()
	var env struct {
		OK     bool               `json:"ok"`
		Result managementResponse `json:"result"`
		Error  map[string]any     `json:"error"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("envelope decode failed: %v (%s)", err, raw)
	}
	if !env.OK {
		t.Fatalf("expected ok envelope, got %v", env.Error)
	}
	return env.Result
}

func register(t *testing.T, app *App, yaml string) {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"config_yaml": []byte(yaml)})
	if err != nil {
		t.Fatalf("register encode failed: %v", err)
	}
	if _, err := app.handle("plugin.register", raw); err != nil {
		t.Fatalf("plugin.register failed: %v", err)
	}
}

func managementCall(t *testing.T, app *App, method, path string) managementResponse {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"Method": method, "Path": path})
	if err != nil {
		t.Fatalf("management encode failed: %v", err)
	}
	out, err := app.handle("management.handle", raw)
	if err != nil {
		t.Fatalf("management.handle failed: %v", err)
	}
	return envelopeResult(t, out)
}

func TestParseConfigDefaults(t *testing.T) {
	cfg, err := parseConfig(nil)
	if err != nil {
		t.Fatalf("defaults must parse: %v", err)
	}
	if cfg != defaultConfig() {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
	cfg, err = parseConfig([]byte("# comment\nservice_url: \"http://127.0.0.1:19000/usage/\"\ncache_seconds: 30\ntimeout_seconds: 3\n"))
	if err != nil {
		t.Fatalf("valid config rejected: %v", err)
	}
	if cfg.ServiceURL != "http://127.0.0.1:19000/usage/" || cfg.CacheSeconds != 30 || cfg.TimeoutSeconds != 3 {
		t.Fatalf("unexpected parse: %+v", cfg)
	}
}

func TestParseConfigRejectsBadValues(t *testing.T) {
	for _, yaml := range []string{
		"service_url: ''\n",
		"service_url: 127.0.0.1:18390/usage/\n",
		"cache_seconds: -1\n",
		"cache_seconds: soon\n",
		"timeout_seconds: 0\n",
		"timeout_seconds: 600\n",
	} {
		if _, err := parseConfig([]byte(yaml)); err == nil {
			t.Fatalf("expected rejection for %q", yaml)
		}
	}
}

func TestRegistrationDeclaresResourceMenu(t *testing.T) {
	regRaw, err := global.handle("plugin.register", []byte(`{"config_yaml":""}`))
	if err != nil {
		t.Fatalf("plugin.register failed: %v", err)
	}
	regText := string(regRaw)
	for _, want := range []string{"management_api", version, "cpa-quota-cards", "service_url"} {
		if !strings.Contains(regText, want) {
			t.Fatalf("plugin metadata missing %q: %s", want, regText)
		}
	}

	raw, err := global.handle("management.register", nil)
	if err != nil {
		t.Fatalf("management.register failed: %v", err)
	}
	text := string(raw)
	for _, want := range []string{`"/dashboard"`, "额度与用量", `/cpa-quota-cards/status`} {
		if !strings.Contains(text, want) {
			t.Fatalf("registration missing %q: %s", want, text)
		}
	}
	if strings.Contains(text, "http://") || strings.Contains(text, "https://") {
		t.Fatalf("host rejects absolute resource paths; registration must not carry a URL in Path: %s", text)
	}
}

func TestPageIsProxiedFromService(t *testing.T) {
	var hits int
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		if r.URL.Path != "/usage/" {
			t.Errorf("unexpected upstream path %q", r.URL.Path)
		}
		_, _ = w.Write([]byte("<!doctype html><title>cards</title>"))
	}))
	defer upstream.Close()

	app := newTestApp()
	register(t, app, "service_url: "+upstream.URL+"/usage/\ncache_seconds: 60\n")

	for _, path := range []string{"/dashboard", "/v0/resource/plugins/" + pluginID + "/dashboard"} {
		resp := managementCall(t, app, http.MethodGet, path)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("%s: status %d", path, resp.StatusCode)
		}
		if !strings.Contains(string(resp.Body), "<title>cards</title>") {
			t.Fatalf("%s: unexpected body %q", path, resp.Body)
		}
		if got := resp.Headers.Get("Content-Security-Policy"); !strings.Contains(got, "frame-ancestors 'self'") {
			t.Fatalf("%s: missing frame policy: %q", path, got)
		}
	}
	if hits != 1 {
		t.Fatalf("cache_seconds should collapse the second request, upstream hits=%d", hits)
	}
}

func TestWrongMethodAndUnknownPath(t *testing.T) {
	app := newTestApp()
	if resp := managementCall(t, app, http.MethodPost, "/dashboard"); resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST /dashboard should be 405, got %d", resp.StatusCode)
	}
	if resp := managementCall(t, app, http.MethodGet, "/nope"); resp.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown path should be 404, got %d", resp.StatusCode)
	}
}

func TestUnreachableServiceRendersExplanation(t *testing.T) {
	app := newTestApp()
	register(t, app, "service_url: http://127.0.0.1:1/usage/\ntimeout_seconds: 1\n")

	resp := managementCall(t, app, http.MethodGet, "/dashboard")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("error page should still render inside the panel, got %d", resp.StatusCode)
	}
	if !strings.Contains(string(resp.Body), "额度服务暂时不可用") {
		t.Fatalf("missing explanation: %q", resp.Body)
	}

	status := managementCall(t, app, http.MethodGet, "/"+pluginID+"/status")
	var decoded map[string]any
	if err := json.Unmarshal(status.Body, &decoded); err != nil {
		t.Fatalf("status decode failed: %v", err)
	}
	if decoded["serving"] != false || decoded["error"] == nil {
		t.Fatalf("status should report the failure: %v", decoded)
	}
	if decoded["cache_seconds"] == nil || decoded["version"] == nil {
		t.Fatalf("status should report the effective configuration: %v", decoded)
	}
}

func TestReconfigureRejectsInvalidConfig(t *testing.T) {
	app := newTestApp()
	register(t, app, "service_url: http://127.0.0.1:1/usage/\n")
	raw, _ := json.Marshal(map[string]any{"config_yaml": []byte("service_url: not-a-url\n")})
	if _, err := app.handle("plugin.reconfigure", raw); err == nil {
		t.Fatal("invalid reconfigure must fail visibly")
	}
	// The previous good configuration must survive a rejected reload.
	if app.cfg.ServiceURL != "http://127.0.0.1:1/usage/" {
		t.Fatalf("config was clobbered by a failed reconfigure: %+v", app.cfg)
	}
}

func TestShutdownIsSafeWithoutWorkers(t *testing.T) {
	app := newTestApp()
	register(t, app, "service_url: http://127.0.0.1:1/usage/\n")
	if _, err := app.handle("plugin.quiesce", nil); err != nil {
		t.Fatalf("quiesce failed: %v", err)
	}
	if _, err := app.handle("plugin.shutdown", nil); err != nil {
		t.Fatalf("shutdown failed: %v", err)
	}
	if _, err := app.handle("nope", nil); err == nil {
		t.Fatal("unsupported method must error")
	}
}
