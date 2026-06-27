#!/usr/bin/env python3
"""
Real-Time Monitor for X402 Web Scraper MCP
===========================================
Shows: service health, wallet balance, recent transactions, access log tail.
Refreshes every 10 seconds. Keep this running in a terminal window.

Usage: python monitor.py
"""

import json, os, sys, time, urllib.request
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TUNNEL_URL = os.environ.get("TUNNEL_URL", "")
WALLET = "0xcf15b97a41022427f50d4bb284c108eb0a716c2b"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RPC = "https://mainnet.base.org"

def rpc(method, params):
    req = urllib.request.Request(RPC,
        data=json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1}).encode(),
        headers={"Content-Type":"application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except: return {}

def balance():
    eth = rpc("eth_getBalance", [WALLET, "latest"])
    eth_wei = int(eth.get("result","0x0"), 16) if eth.get("result") else 0
    eth_val = eth_wei / 1e18
    usdc_data = "0x70a08231" + "0"*24 + WALLET[2:].lower().zfill(64)
    usdc_r = rpc("eth_call", [{"to": USDC, "data": usdc_data}, "latest"])
    usdc_raw = int(usdc_r.get("result","0x0"), 16) if usdc_r.get("result") else 0
    usdc_val = usdc_raw / 1e6
    return eth_val, usdc_val, usdc_raw

def health():
    if not TUNNEL_URL:
        return "NO_URL"
    try:
        r = urllib.request.urlopen(f"{TUNNEL_URL}/health", timeout=5)
        return "OK" if r.status == 200 else f"HTTP{r.status}"
    except: return "DOWN"

def access_tail(n=5):
    log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "access.log")
    if not os.path.exists(log): return []
    with open(log, encoding="utf-8") as f:
        lines = f.readlines()
    return lines[-n:]

def clear():
    os.system("cls" if sys.platform == "win32" else "clear")

while True:
    clear()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    eth, usdc, usdc_raw = balance()
    h = health()

    print("=" * 55)
    print("  X402 Web Scraper MCP — REAL-TIME MONITOR")
    print(f"  {ts}")
    print("=" * 55)
    print()
    print(f"  Service:    {'✅ ONLINE' if h == 'OK' else '❌ ' + h}")
    print(f"  Wallet:     {WALLET}")
    print(f"  ETH:        {eth:.6f} ETH")
    print(f"  USDC:       {usdc:.6f} USDC ({usdc_raw} micro)")
    print()
    if usdc_raw == 0:
        print(f"  ⚠️  钱包余额为 0，尚无入账")
    else:
        print(f"  💰 已收到 {usdc:.6f} USDC！")
    print()
    print("  --- Recent Access Log ---")
    for line in access_tail(8):
        print(f"  {line.strip()}")
    print()
    print("  GitHub:    github.com/dji112062126-cyber/x402-web-scraper-mcp")
    print("  Smithery:  smithery.ai/server/dji112062126/web-scraper-x402")
    print("  Press Ctrl+C to exit")
    print("=" * 55)

    time.sleep(10)
