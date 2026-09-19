#!/usr/bin/env python3
"""List CPA plugins: id / enabled / registered / sidebar menus (no secrets printed)."""
import json
import pathlib
import urllib.request

ENV = pathlib.Path("/opt/cpa-manager-plus/.env")
BASE = "http://127.0.0.1:8317"


def key():
    for line in ENV.read_text().splitlines():
        if line.startswith("CPA_MANAGEMENT_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("CPA_MANAGEMENT_KEY not found")


request = urllib.request.Request(
    BASE + "/v0/management/plugins",
    headers={"Authorization": "Bearer " + key()})
payload = json.load(urllib.request.urlopen(request))
items = payload if isinstance(payload, list) else (
    payload.get("plugins") or payload.get("items") or [])
for plugin in items:
    menus = [(m.get("menu"), m.get("path")) for m in (plugin.get("menus") or [])]
    print("{:24} enabled={:5} registered={:5} menus={}".format(
        str(plugin.get("id")), str(plugin.get("enabled")),
        str(plugin.get("registered")), menus))
