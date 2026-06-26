#!/usr/bin/env python3
"""
One-Click Deploy Script for x402 Web Scraper MCP
==================================================
Starts the server, tunnel, and publishes to marketplaces.

Usage:
  python deploy.py              # start server + tunnel + sync config
  python deploy.py --smithery   # also publish to Smithery (needs SMITHERY_API_KEY)
  python deploy.py --coinbase   # prepare Coinbase submission
  python deploy.py --all        # everything
"""

from __future__ import annotations

import sys
# Fix Unicode on Windows GBK console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "mcp_config.json"
MANIFEST_FILE = PROJECT_DIR / "agent_manifest.json"
TUNNEL_URL_FILE = PROJECT_DIR / ".tunnel_url"

# ---- Detect and start tunnel ----


async def find_tunnel_url() -> str | None:
    """Try to detect an existing tunnel URL."""
    # Check localhost.run (on port 4040 for new versions... actually no)
    # Check serveo.net output
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            # Try ngrok API
            resp = await c.get("http://127.0.0.1:4040/api/tunnels")
            if resp.status_code == 200:
                tunnels = resp.json().get("tunnels", [])
                for t in tunnels:
                    url = t.get("public_url", "")
                    if url.startswith("https://"):
                        return url
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=3) as c:
            resp = await c.get("http://127.0.0.1:4041/api/tunnels")
            if resp.status_code == 200:
                tunnels = resp.json().get("tunnels", [])
                for t in tunnels:
                    url = t.get("public_url", "")
                    if url.startswith("https://"):
                        return url
    except Exception:
        pass

    # Check saved URL
    if TUNNEL_URL_FILE.exists():
        return TUNNEL_URL_FILE.read_text().strip()

    return None


async def start_serveo_tunnel(port: int = 8000) -> str:
    """Start a free serveo.net SSH tunnel and return the public URL."""
    import tempfile

    print("[tunnel] Starting serveo.net tunnel (free, no account needed)...")
    print(f"[tunnel] SSH forwarding port {port}...")

    # serveo outputs the URL on stderr/stdout
    proc = await asyncio.create_subprocess_exec(
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-R", f"80:localhost:{port}",
        "serveo.net",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    url = None
    try:
        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                print(f"  [serveo] {text}")
            # Extract URL pattern: serveo uses multiple domains
            import re
            m = re.search(r"(https?://[\w-]+\.(?:lhr\.life|serveo\.net|serveousercontent\.com))", text)
            if m:
                url = m.group(1)
                print(f"\n[tunnel] PUBLIC URL: {url}")
                break
    except asyncio.CancelledError:
        pass

    if url:
        TUNNEL_URL_FILE.write_text(url)
        return url
    return None


def update_configs(public_url: str) -> None:
    """Write the public URL into config and manifest files."""
    base = public_url.rstrip("/")
    sse_url = f"{base}/mcp/sse"

    # mcp_config.json
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for entry in config.get("mcpServers", {}).values():
            entry["url"] = sse_url
        CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[config] mcp_config.json → {sse_url}")

    # agent_manifest.json
    if MANIFEST_FILE.exists():
        manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        manifest["protocol"]["endpoint"] = sse_url
        manifest["protocol"]["endpoint_note"] = f"Live tunnel URL: {sse_url}"
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[config] agent_manifest.json → {sse_url}")


# ---- Health checks ----


async def health_check(url: str) -> bool:
    """Verify the server is reachable."""
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{url}/health")
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception as e:
            print(f"[health] FAIL: {e}")
            return False


# ---- Smithery publish ----


def publish_smithery(public_url: str) -> int:
    """Publish to Smithery using their CLI."""
    api_key = os.environ.get("SMITHERY_API_KEY")
    if not api_key:
        print("\n[smithery] To publish: Set SMITHERY_API_KEY env var and run:")
        print(f"  smithery mcp publish {public_url} -n dji112062126/web-scraper-x402")
        print("  Get your API key: https://smithery.ai/account/api-keys")
        return 1

    result = subprocess.run(
        [
            "smithery", "mcp", "publish",
            public_url,
            "-n", "dji112062126/web-scraper-x402",
            "--json",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "SMITHERY_API_KEY": api_key},
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"[smithery] Publish error: {result.stderr}")
    return result.returncode


# ---- Coinbase submission ----


def prepare_coinbase_submission() -> int:
    """Prepare the Coinbase Agent Marketplace submission package."""
    print("\n[coinbase] Preparing Agent Marketplace submission...")

    if not MANIFEST_FILE.exists():
        print("[coinbase] ERROR: agent_manifest.json not found")
        return 1

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))

    # Validate required fields
    required = {
        "agent.name": manifest.get("agent", {}).get("name"),
        "blockchain.recipient_address": manifest.get("blockchain", {}).get("recipient_address"),
        "pricing.price_per_call": manifest.get("pricing", {}).get("price_per_call"),
        "protocol.endpoint": manifest.get("protocol", {}).get("endpoint"),
    }
    for key, val in required.items():
        if not val:
            print(f"[coinbase] ERROR: Missing required field: {key}")
            return 1

    print(f"[coinbase] ✓ name: {required['agent.name']}")
    print(f"[coinbase] ✓ recipient: {required['blockchain.recipient_address']}")
    print(f"[coinbase] ✓ price: {required['pricing.price_per_call']} USDC")
    print(f"[coinbase] ✓ endpoint: {required['protocol.endpoint']}")

    # Write submission summary
    summary = {
        "submission_date": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest": manifest,
        "source_code": "https://github.com/dji112062126-cyber/first-cc",
        "server_status": "running",
        "verification_instructions": {
            "health_check": f"curl {required['protocol.endpoint'].replace('/mcp/sse','')}/health",
            "payment_info": f"curl {required['protocol.endpoint'].replace('/mcp/sse','')}/payment-info",
            "mcp_sse": required['protocol.endpoint'],
            "free_tool_test": "mcp tools/list should return 2 tools without payment",
            "paid_tool_test": "mcp tools/call scrape_webpage requires X-402-Payment header",
        },
    }

    out_path = PROJECT_DIR / "coinbase_submission.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[coinbase] Submission package written to: {out_path}")
    print("\n  Submit at: https://developers.coinbase.com/agent-marketplace")
    print("  (or the appropriate Coinbase Developer Portal endpoint)")
    return 0


# ---- Main ----


async def main() -> int:
    parser = argparse.ArgumentParser(description="One-Click Deploy for x402 Web Scraper MCP")
    parser.add_argument("--smithery", action="store_true", help="Publish to Smithery")
    parser.add_argument("--coinbase", action="store_true", help="Prepare Coinbase submission")
    parser.add_argument("--all", action="store_true", help="Everything")
    parser.add_argument("--tunnel-only", action="store_true", help="Just start tunnel + update configs")
    args = parser.parse_args()

    do_all = args.all
    do_smithery = args.smithery or do_all
    do_coinbase = args.coinbase or do_all

    # 1. Find or start tunnel
    print("=" * 60)
    print("  x402 Web Scraper MCP — One-Click Deploy")
    print("=" * 60)

    public_url = await find_tunnel_url()
    if public_url:
        print(f"[tunnel] Found existing tunnel: {public_url}")
    else:
        print("[tunnel] No tunnel found. Starting serveo.net tunnel...")
        public_url = await start_serveo_tunnel(8000)

    if not public_url:
        print("[tunnel] ERROR: Could not establish tunnel.")
        print("  Start manually: ssh -R 80:localhost:8000 serveo.net")
        print("  Or: ngrok http 8000")
        return 1

    update_configs(public_url)

    # 2. Health check
    print(f"\n[health] Checking {public_url} ...")
    ok = await health_check(public_url)
    if ok:
        print(f"[health] [OK] Server is reachable and healthy!")
    else:
        print(f"[health] ✗ Server not reachable. Start it: python server.py")
        return 1

    # 3. Smithery
    if do_smithery:
        publish_smithery(public_url)

    # 4. Coinbase
    if do_coinbase:
        prepare_coinbase_submission()

    # 5. Summary
    print("\n" + "=" * 60)
    print("  DEPLOY SUMMARY")
    print("=" * 60)
    print(f"  Public URL:     {public_url}")
    print(f"  MCP SSE:        {public_url.rstrip('/')}/mcp/sse")
    print(f"  Health:         {public_url.rstrip('/')}/health")
    print(f"  Payment Info:   {public_url.rstrip('/')}/payment-info")
    print(f"  GitHub:         https://github.com/dji112062126-cyber/first-cc")
    if do_smithery:
        print(f"  Smithery:       https://smithery.ai/server/dji112062126/web-scraper-x402")
    print(f"  Config:         {CONFIG_FILE}")
    print(f"  Manifest:       {MANIFEST_FILE}")
    print(f"  Access Log:     {PROJECT_DIR / 'access.log'}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
