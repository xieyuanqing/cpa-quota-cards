#!/usr/bin/env python3
"""Install or upgrade the cpa-quota-cards CLIProxyAPI plugin (guarded).

What it touches:
  $CPA_ROOT/plugins/linux/amd64/cpa-quota-cards-v<version>.so   (new file only)
  $CPA_ROOT/config.yaml  -> plugins.configs."cpa-quota-cards"   (one block)

Why it is written this way:
  * config.yaml is bind-mounted into the CLIProxyAPI container. Rewriting it
    atomically (os.replace / mv) leaves the container pinned to the *deleted*
    inode: the container keeps running on the old config while the host sees a
    new file, and every later change lands in the wrong place. This script
    always rewrites in place (same inode) and warns if it detects a stale mount.
  * The management API cannot create a config entry for a plugin id it has not
    discovered yet (PUT/PATCH answer 404). So the library is copied first, the
    config block is written on disk, and the API is only used to hot-load the
    plugin once the host knows the id.

Usage:
  python3 scripts/install_plugin.py                     # install, then verify
  python3 scripts/install_plugin.py --upgrade           # replace with a newer .so
  python3 scripts/install_plugin.py --status            # print plugin status JSON
  python3 scripts/install_plugin.py --disable           # turn the plugin off
  python3 scripts/install_plugin.py --dry-run           # validate, change nothing
"""
import argparse
import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import yaml

ID = "cpa-quota-cards"

CPA_ROOT = pathlib.Path(os.environ.get("CPA_ROOT", "/root/CLIProxyAPI"))
def _base_url(name, default):
    """Read a base URL and drop any path: a stray API prefix must not leak in."""
    value = os.environ.get(name, default).strip().rstrip("/")
    parts = urlsplit(value)
    if parts.path and parts.path != "/":
        value = "{}://{}".format(parts.scheme, parts.netloc)
    return value


CPA_BASE = _base_url("CPA_QUOTA_CARDS_MGMT_BASE", _base_url("CPA_BASE_URL", "http://127.0.0.1:8317"))
CONFIG = CPA_ROOT / "config.yaml"
LIB_DIR = CPA_ROOT / "plugins" / "linux" / "amd64"
BACKUP_DIR = pathlib.Path(os.environ.get("CPA_QUOTA_CARDS_BACKUP_DIR", "/root/backups"))
REPO = pathlib.Path(__file__).resolve().parent.parent
DIST = REPO / "dist"


class ApiError(RuntimeError):
    def __init__(self, code, method, route, detail):
        super().__init__("{} {} failed: HTTP {} {}".format(method, route, code, detail))
        self.code = code


def management_key():
    candidates = [pathlib.Path(os.environ.get("CPA_ENV_FILE", "")) if os.environ.get("CPA_ENV_FILE") else None,
                  pathlib.Path("/opt/cpa-manager-plus/.env"),
                  CPA_ROOT / ".env"]
    for path in candidates:
        if not path or not path.exists():
            continue
        for line in path.read_text().splitlines():
            if line.startswith("CPA_MANAGEMENT_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("CPA_MANAGEMENT_KEY not found (set CPA_ENV_FILE)")


def call(method, route, payload=None, timeout=70, attempts=1):
    """Management API call.

    404/503 on plugin routes is a known transient: the host re-registers the
    per-plugin routes while it reloads the plugin host, and until that pass
    finishes /v0/management/plugins/<id>/* answers 404 with an empty body. A
    plain list request kicks discovery, so retry after one.
    """
    data = None if payload is None else json.dumps(payload).encode()
    last = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            "{}/v0/management/{}".format(CPA_BASE, route), data=data, method=method,
            headers={"Authorization": "Bearer " + management_key(),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode() or "{}"
                return json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace").strip()
            if error.code in (404, 503) and attempt + 1 < attempts:
                last = "HTTP {} {}".format(error.code, detail)
                try:  # kick: force the host to re-register plugin routes
                    kick = urllib.request.Request(
                        "{}/v0/management/plugins".format(CPA_BASE),
                        headers={"Authorization": "Bearer " + management_key()})
                    urllib.request.urlopen(kick, timeout=15).read()
                except Exception:
                    pass
                time.sleep(1.0 + attempt)
                continue
            raise ApiError(error.code, method, route, detail) from None
        except urllib.error.URLError as error:
            raise SystemExit("management API unreachable at {}: {}".format(CPA_BASE, error)) from None
    raise SystemExit("management API kept failing: " + str(last))


def read_config():
    return yaml.safe_load(CONFIG.read_text()) or {}


def installed_versions():
    return sorted(p.name for p in LIB_DIR.glob(ID + "-v*.so"))


def newest_library(explicit=None):
    if explicit:
        path = pathlib.Path(explicit)
        if not path.exists():
            raise SystemExit("library not found: " + str(path))
        return path
    candidates = sorted(DIST.glob(ID + "-v*.so"),
                        key=lambda p: [int(x) for x in re.findall(r"\d+", p.name)])
    if not candidates:
        raise SystemExit("no " + ID + "-v*.so in " + str(DIST) + " (run plugin/scripts/build.sh)")
    return candidates[-1]


def backup_config():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / ("config.yaml." + stamp + ".pre-" + ID)
    shutil.copy2(CONFIG, target)
    target.chmod(0o600)
    return target


def write_config_in_place(text):
    """Rewrite config.yaml without changing its inode (bind-mounted file)."""
    inode_before = CONFIG.stat().st_ino
    with open(CONFIG, "w") as handle:
        handle.write(text)
    if CONFIG.stat().st_ino != inode_before:
        raise SystemExit("config.yaml inode changed unexpectedly")
    return yaml.safe_load(text)


def upsert_block(service_url, cache_seconds, timeout_seconds):
    text = CONFIG.read_text()
    before = yaml.safe_load(text) or {}
    block = {"enabled": True, "service_url": service_url,
             "cache_seconds": cache_seconds, "timeout_seconds": timeout_seconds}

    if ID in (before.get("plugins", {}) or {}).get("configs", {}):
        existing = before["plugins"]["configs"][ID]
        if existing.get("store"):
            block = dict(existing, **block)
        before["plugins"]["configs"][ID] = block
        after = yaml.safe_dump(before, sort_keys=False, allow_unicode=True)
        result = write_config_in_place(after)
    else:
        lines = text.splitlines(keepends=True)
        anchor = None
        for index, line in enumerate(lines):
            if re.match(r"^\s*configs:\s*$", line):
                anchor = index
                break
        if anchor is None:
            if not text.endswith("\n"):
                text += "\n"
            text += "plugins:\n  configs:\n"
            anchor = len(text.splitlines()) - 1
        indent = len(lines[anchor]) - len(lines[anchor].lstrip()) + 2
        pad = " " * indent
        injected = (
            "{}{}:\n".format(pad, ID)
            + "{}enabled: true\n".format(pad + "  ")
            + "{}service_url: {}\n".format(pad + "  ", service_url)
            + "{}cache_seconds: {}\n".format(pad + "  ", cache_seconds)
            + "{}timeout_seconds: {}\n".format(pad + "  ", timeout_seconds))
        lines.insert(anchor + 1, injected)
        result = write_config_in_place("".join(lines))

    previous = copy.deepcopy(before)
    previous.get("plugins", {}).get("configs", {}).pop(ID, None)
    current = copy.deepcopy(result)
    current.get("plugins", {}).get("configs", {}).pop(ID, None)
    if previous != current:
        raise SystemExit("refusing to continue: unrelated configuration changed")
    return result["plugins"]["configs"][ID]


def wait_registered(deadline_seconds=45):
    deadline = time.time() + deadline_seconds
    last = ""
    while time.time() < deadline:
        try:
            return call("GET", ID + "/status", timeout=10)
        except ApiError as error:
            last = str(error)
            time.sleep(2)
    return {"serving": False, "error": "not registered within {}s ({})".format(deadline_seconds, last)}


def mount_is_stale():
    """True when the container's config.yaml is no longer the host's inode."""
    try:
        inside = subprocess.run(
            ["docker", "exec", "cli-proxy-api", "cat", "/CLIProxyAPI/config.yaml"],
            capture_output=True, check=True).stdout
    except Exception:
        return None
    host = CONFIG.read_bytes()
    return hashlib.sha256(inside).hexdigest() != hashlib.sha256(host).hexdigest()


def warn_if_stale():
    stale = mount_is_stale()
    if stale:
        print("WARNING: the CLIProxyAPI container is running an older config.yaml")
        print("         (its bind mount is pinned to a replaced inode).")
        print("         Disk state is correct; run `docker restart cli-proxy-api` so the")
        print("         container picks it up, otherwise panel-side changes will be lost.")
    return stale


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-url", default=os.environ.get(
        "CPA_QUOTA_CARDS_SERVICE_URL", "http://172.29.0.1:18390/usage/"))
    parser.add_argument("--cache-seconds", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=8)
    parser.add_argument("--library", help="path to the .so (default: newest in dist/)")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(call("GET", ID + "/status"), ensure_ascii=False, indent=2))
        return 0
    if args.disable:
        call("PATCH", "plugins/{}/enabled".format(ID), {"enabled": False})
        config = read_config()
        block = config["plugins"]["configs"][ID]
        text = CONFIG.read_text()
        after = yaml.safe_dump(
            {**config, "plugins": {"configs": {**config["plugins"]["configs"],
                                               ID: {**block, "enabled": False}}}},
            sort_keys=False, allow_unicode=True)
        write_config_in_place(after or text)
        print("disabled {} (re-enable with: python3 {} --upgrade)".format(ID, sys.argv[0]))
        warn_if_stale()
        return 0

    library = newest_library(args.library)
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    destination = LIB_DIR / library.name
    configured = ID in (read_config().get("plugins", {}) or {}).get("configs", {})

    print("library    : {} (sha256 {})".format(library, digest[:16]))
    print("destination: {}".format(destination))
    print("installed  : {}".format(installed_versions() or "none"))
    print("configured : {}".format(configured))

    if configured and not args.upgrade:
        raise SystemExit("{} is already configured - use --upgrade or --status".format(ID))
    if destination.exists() and args.upgrade:
        print("note: {} already exists (same version) - nothing to copy".format(destination.name))
    if args.dry_run:
        print("dry run: no changes made")
        return 0

    backup = backup_config()
    print("backup     : {}".format(backup))

    if not destination.exists():
        shutil.copy2(library, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise SystemExit("library copy failed verification")
        print("copied     : {}".format(destination.name))

    block = upsert_block(args.service_url, args.cache_seconds, args.timeout_seconds)
    print("config     : {}".format(json.dumps(block, ensure_ascii=False)))

    print("waiting for the host to load the plugin ...")
    expected = re.search(r"-v(\d+\.\d+\.\d+)\.so$", library.name)
    expected = expected.group(1) if expected else ""
    status = wait_registered()
    if not status.get("serving") or (expected and status.get("version") != expected):
        print("reloading via the management API (disable/enable) ...")
        call("PATCH", "plugins/{}/enabled".format(ID), {"enabled": False}, attempts=10)
        call("PATCH", "plugins/{}/enabled".format(ID), {"enabled": True}, attempts=10)
        status = wait_registered()
    print("status     : {}".format(json.dumps(status, ensure_ascii=False)))

    stale = warn_if_stale()
    if not status.get("serving"):
        print("FAILED: the plugin is not serving its page")
        print("        check `docker logs --tail 50 cli-proxy-api`")
        return 1
    if expected and status.get("version") != expected:
        print("FAILED: running version {} but {} is installed".format(
            status.get("version"), expected))
        print("        docker restart cli-proxy-api re-runs plugin discovery")
        return 1
    print("OK: plugin page registered at /v0/resource/plugins/{}/dashboard".format(ID))
    print("    sidebar entry: 额度与用量 (after a panel reload)")
    if stale:
        print("    reminder: docker restart cli-proxy-api to re-sync the config mount")
    return 0


if __name__ == "__main__":
    sys.exit(main())
