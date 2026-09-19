# Third-party notices

## CLIProxyAPI plugin ABI / examples

`plugin/bridge.go` is derived from the MIT-licensed plugin examples shipped with
CLIProxyAPI (https://github.com/router-for-me/CLIProxyAPI): the C-ABI
`cliproxy_plugin_init` entry point, the host/plugin struct layout and the
Go-side trampolines follow that reference implementation.

    MIT License — Copyright (c) CLIProxyAPI contributors

## cpa-window-keeper (design reference)

The dashboard's visual language (glass/iOS-style cards, `cli-proxy-theme`
variables, `data-theme` handling) and the panel-login-state reuse are modelled
on cpa-window-keeper (https://github.com/xieyuanqing/cpa-window-keeper), MIT.

    MIT License — Copyright (c) 2026 xieyuanqing (晴空)

## Model prices

Per-token prices come from the CPAMP database (`model_prices` table), which is
populated from models.dev (https://models.dev). No price table is vendored in
this repository.

## Panel storage format

The panel-login-state reuse reads CPAMP's obfuscated `localStorage` entries
(`enc::v1::` / `enc::v2::`: XOR with a key derived from the host name). The
format is CPAMP's, not ours; it is only read, never written.
