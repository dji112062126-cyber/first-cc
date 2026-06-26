#!/usr/bin/env python3
"""
Tunnel URL Auto-Sync for MCP Config
====================================
Detects the live public URL from ngrok or cpolar and automatically
updates ``mcp_config.json`` so external clients can reach the MCP server.

Supported tunnel providers:
  - ngrok   (local API: http://127.0.0.1:4040/api/tunnels)
  - cpolar  (local API: http://127.0.0.1:4041/api/tunnels)

Usage:
  # Run once to sync the current tunnel URL
  python update_config.py

  # Watch mode — re-check every 60 seconds
  python update_config.py --watch

Principles:
  - Only overwrites the "url" field; all other config is preserved.
  - Prints a diff showing old → new URL.
  - Non-destructive: if no tunnel is running the file is left unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import httpx


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "mcp_config.json"

# ---------------------------------------------------------------------------
# Tunnel auto-detection
# ---------------------------------------------------------------------------

TUNNEL_PROBES: list[dict] = [
    {
        "name": "ngrok",
        "api_url": "http://127.0.0.1:4040/api/tunnels",
        "extractor": lambda data: _extract_ngrok(data),
    },
    {
        "name": "cpolar",
        "api_url": "http://127.0.0.1:4041/api/tunnels",
        "extractor": lambda data: _extract_cpolar(data),
    },
]


def _extract_ngrok(data: dict) -> Optional[str]:
    """Extract the first HTTPS public URL from ngrok API response."""
    tunnels = data.get("tunnels", [])
    for t in tunnels:
        url = t.get("public_url", "")
        if url.startswith("https://"):
            return url
    # Fallback to http:// if no https tunnel
    for t in tunnels:
        url = t.get("public_url", "")
        if url.startswith("http://"):
            return url
    return None


def _extract_cpolar(data: dict) -> Optional[str]:
    """Extract the first HTTPS public URL from cpolar API response."""
    tunnels = data.get("tunnels", [])
    for t in tunnels:
        url = t.get("public_url", "")
        if url.startswith("https://"):
            return url
    for t in tunnels:
        url = t.get("public_url", "")
        if url.startswith("http://"):
            return url
    return None


async def detect_tunnel_url() -> Optional[str]:
    """Probe known tunnel providers and return the first HTTPS URL found."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        for probe in TUNNEL_PROBES:
            try:
                resp = await client.get(probe["api_url"])
                if resp.status_code == 200:
                    data = resp.json()
                    url = probe["extractor"](data)
                    if url:
                        print(f"[detect] Found {probe['name']} tunnel: {url}")
                        return url
            except (httpx.ConnectError, httpx.TimeoutException, Exception):
                continue
    return None


# ---------------------------------------------------------------------------
# Config update
# ---------------------------------------------------------------------------


def update_config(new_public_url: str) -> bool:
    """
    Update the ``url`` field inside the first mcpServer entry
    of mcp_config.json.  Returns True if changed, False if no-op.
    """
    if not CONFIG_PATH.exists():
        print(f"[error] {CONFIG_PATH} does not exist. Run server.py first to generate it.")
        return False

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    # Navigate to the server entry — key may vary
    servers = config.get("mcpServers", {})
    if not servers:
        print("[warn] mcp_config.json has no mcpServers entries")
        return False

    # Take the first server entry
    server_name = next(iter(servers))
    server_entry = servers[server_name]

    old_url = server_entry.get("url", "(not set)")
    new_url = new_public_url.rstrip("/") + "/mcp/sse"

    if old_url == new_url:
        print(f"[skip] URL already up-to-date: {new_url}")
        return False

    server_entry["url"] = new_url
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[updated] {CONFIG_PATH.name}")
    print(f"  old → {old_url}")
    print(f"  new → {new_url}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def run_once() -> int:
    """Single-shot sync."""
    print("Probing for tunnel URLs...")
    url = await detect_tunnel_url()
    if url is None:
        print("[warn] No tunnel detected.")
        print("       Is ngrok or cpolar running?")
        print("       Start ngrok:  ngrok http 8000")
        print("       Start cpolar: cpolar http 8000")
        return 1
    changed = update_config(url)
    if changed:
        print("[done] mcp_config.json updated for external access.")
    return 0


async def run_watch(interval: int = 60) -> None:
    """Watch mode — poll the tunnel API every `interval` seconds."""
    print(f"[watch] Polling for tunnel URL every {interval}s (Ctrl+C to stop)")
    last_url: Optional[str] = None
    while True:
        try:
            url = await detect_tunnel_url()
            if url and url != last_url:
                update_config(url)
                last_url = url
            elif url is None and last_url is not None:
                print("[warn] Tunnel went away — old URL preserved in config")
                last_url = None
            else:
                print(f"[watch] {time.strftime('%H:%M:%S')} — no change")
        except KeyboardInterrupt:
            print("\n[watch] Stopped.")
            return
        except Exception as exc:
            print(f"[watch] Error: {exc}")
        await _async_sleep(interval)


async def _async_sleep(seconds: float) -> None:
    """Async sleep helper (3.11+ compatible)."""
    import asyncio
    await asyncio.sleep(seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Auto-detect tunnel URL and update mcp_config.json"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode: poll every 60s and update on change",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds for --watch (default: 60)",
    )
    args = parser.parse_args()

    import asyncio

    if args.watch:
        asyncio.run(run_watch(args.interval))
    else:
        sys.exit(asyncio.run(run_once()))
