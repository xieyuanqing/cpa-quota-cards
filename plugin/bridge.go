// The C ABI bridge follows CLIProxyAPI's MIT-licensed plugin examples.
// See THIRD_PARTY_LICENSES.md for attribution.
package main

/*
#include <stdint.h>
#include <stdlib.h>
typedef struct { void* ptr; size_t len; } cliproxy_buffer;
typedef int (*cliproxy_host_call_fn)(void*, const char*, const uint8_t*, size_t, cliproxy_buffer*);
typedef void (*cliproxy_host_free_fn)(void*, size_t);
typedef struct { uint32_t abi_version; void* host_ctx; cliproxy_host_call_fn call; cliproxy_host_free_fn free_buffer; } cliproxy_host_api;
typedef int (*cliproxy_plugin_call_fn)(char*, uint8_t*, size_t, cliproxy_buffer*);
typedef void (*cliproxy_plugin_free_fn)(void*, size_t);
typedef void (*cliproxy_plugin_shutdown_fn)(void);
typedef struct { uint32_t abi_version; cliproxy_plugin_call_fn call; cliproxy_plugin_free_fn free_buffer; cliproxy_plugin_shutdown_fn shutdown; } cliproxy_plugin_api;
extern int cliproxyPluginCall(char*, uint8_t*, size_t, cliproxy_buffer*);
extern void cliproxyPluginFree(void*, size_t);
extern void cliproxyPluginShutdown(void);
static const cliproxy_host_api* stored_host;
static void store_host(const cliproxy_host_api* h) { stored_host=h; }
*/
import "C"

import (
	"encoding/json"
	"unsafe"
)

func main() {}

//export cliproxy_plugin_init
func cliproxy_plugin_init(host *C.cliproxy_host_api, plugin *C.cliproxy_plugin_api) C.int {
	if host == nil || plugin == nil || host.abi_version != 1 {
		return 1
	}
	C.store_host(host)
	plugin.abi_version = 1
	plugin.call = C.cliproxy_plugin_call_fn(C.cliproxyPluginCall)
	plugin.free_buffer = C.cliproxy_plugin_free_fn(C.cliproxyPluginFree)
	plugin.shutdown = C.cliproxy_plugin_shutdown_fn(C.cliproxyPluginShutdown)
	return 0
}

//export cliproxyPluginCall
func cliproxyPluginCall(method *C.char, request *C.uint8_t, n C.size_t, response *C.cliproxy_buffer) (rc C.int) {
	if response == nil {
		return 1
	}
	response.ptr = nil
	response.len = 0
	defer func() {
		if recover() != nil {
			raw := errorEnvelope("plugin panic; see tests before retrying")
			response.ptr = C.CBytes(raw)
			response.len = C.size_t(len(raw))
			rc = 1
		}
	}()
	if method == nil || n > 8<<20 {
		return 1
	}
	var input []byte
	if request != nil && n > 0 {
		input = C.GoBytes(unsafe.Pointer(request), C.int(n))
	}
	output, err := global.handle(C.GoString(method), input)
	if err != nil {
		output = errorEnvelope(err.Error())
		rc = 1
	}
	response.ptr = C.CBytes(output)
	response.len = C.size_t(len(output))
	return rc
}

//export cliproxyPluginFree
func cliproxyPluginFree(p unsafe.Pointer, n C.size_t) {
	if p != nil {
		C.free(p)
	}
}

//export cliproxyPluginShutdown
func cliproxyPluginShutdown() { global.shutdown(); C.store_host(nil) }

func okEnvelope(result any) ([]byte, error) {
	return json.Marshal(map[string]any{"ok": true, "result": result})
}

func errorEnvelope(message string) []byte {
	b, _ := json.Marshal(map[string]any{"ok": false, "error": map[string]any{"code": "cpa_quota_cards_error", "message": message}})
	return b
}
