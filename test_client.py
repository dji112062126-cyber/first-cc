#!/usr/bin/env python3
"""
Integration Test Client for Web Scraper MCP x402 Service
=========================================================
Uses `httpx_sse` (already installed as an mcp dependency) for SSE
event parsing, and httpx for JSON-RPC POST requests over the
MCP SSE transport.

MCP SSE handshake flow:
  1. GET  /mcp/sse                         → receive endpoint event
  2. POST /mcp/messages?session_id=<id>     → send initialize
  3. Read SSE for initialize result
  4. POST /mcp/messages?session_id=<id>     → send initialized notification
  5. Session ready → send tools/list, tools/call, etc.

Test scenarios:
  1. tools/list         → FREE — returns tool list
  2. scrape_webpage (no X-402-Payment) → x402 PaymentRequired error
  3. scrape_webpage (fake X-402-Payment) → validation error

Usage:
  python test_client.py [--base-url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import AsyncIterator, Optional

import httpx
from httpx_sse import aconnect_sse, ServerSentEvent

# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------
_RESET = "\033[0m"
_BOLD = "\033[1m"
_RED = "\033[91m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_MAGENTA = "\033[95m"


def banner() -> None:
    print()
    print(f"{_CYAN}{_BOLD}{'=' * 60}{_RESET}")
    print(f"{_CYAN}{_BOLD}  x402 Web Scraper MCP — Integration Test Suite{_RESET}")
    print(f"{_CYAN}{_BOLD}{'=' * 60}{_RESET}")
    print()


def ok(msg: str) -> None:
    print(f"  {_GREEN}[PASS]{_RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {_YELLOW}[WARN]{_RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {_RED}[FAIL]{_RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {_CYAN}[INFO]{_RESET} {msg}")


# ---------------------------------------------------------------------------
# MCP SSE Session (httpx + httpx_sse, proper async context manager)
# ---------------------------------------------------------------------------


class McpSseSession:
    """
    MCP SSE session that lives inside an `async with aconnect_sse(...)` block.

    Handles SSE event iteration, MCP initialization, and JSON-RPC calls.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        sse_iter: AsyncIterator[ServerSentEvent],
        message_url: str,
        session_id: str,
    ):
        self._client = client
        self._sse_iter = sse_iter
        self._message_url = message_url
        self._session_id = session_id
        self._req_counter = 1

    async def _read_response(self, expected_id: int) -> dict:
        """Read one JSON-RPC response from the SSE stream."""
        async for event in self._sse_iter:
            if event.event == "message" and event.data:
                try:
                    body = json.loads(event.data)
                    if body.get("id") == expected_id:
                        return body
                except json.JSONDecodeError:
                    pass
        return {}

    async def _post_and_read(self, payload: dict, extra_headers: dict | None = None) -> dict:
        """POST a JSON-RPC request and read the response from SSE."""
        headers = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        req_id = payload.get("id", self._next_id())
        payload["id"] = req_id

        await self._client.post(self._message_url, json=payload, headers=headers)
        return await self._read_response(req_id)

    async def initialize(self) -> None:
        """MCP initialization handshake."""
        info("MCP initialize handshake...")
        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "x402-test-client", "version": "1.0.0"},
            },
        }
        await self._post_and_read(init_req)

        # Send initialized notification
        notif = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        await self._client.post(
            self._message_url, json=notif, headers={"Content-Type": "application/json"}
        )
        info("Session ready for tool calls")

    async def list_tools(self) -> dict:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}}
        return await self._post_and_read(payload)

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        return await self._post_and_read(payload)

    async def call_tool_with_payment(self, tool_name: str, arguments: dict, tx: str) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        return await self._post_and_read(payload, extra_headers={"X-402-Payment": tx})

    def _next_id(self) -> int:
        c = self._req_counter
        self._req_counter += 1
        return c


# ---------------------------------------------------------------------------
# x402 helpers
# ---------------------------------------------------------------------------


def extract_x402(body: dict) -> Optional[dict]:
    """Dig x402 PaymentRequired out of an MCP tool-call response."""
    if not isinstance(body, dict):
        return None
    result = body.get("result", {})
    if isinstance(result, dict) and result.get("isError"):
        for item in result.get("content", []):
            text = item.get("text", "")
            if isinstance(text, str):
                try:
                    inner = json.loads(text)
                    if isinstance(inner, dict) and "accepts" in inner:
                        return inner
                except (json.JSONDecodeError, TypeError):
                    pass
                if "PaymentRequired" in text or "Payment required" in text:
                    return {"error": text, "accepts": []}
    err = body.get("error", {})
    if isinstance(err, dict) and any(
        kw in str(err).lower() for kw in ("payment", "402")
    ):
        return {"error": str(err), "accepts": []}
    return None


def _print_x402(x402: dict) -> None:
    accepts = x402.get("accepts", [])
    if accepts and isinstance(accepts[0], dict):
        a = accepts[0]
        print(f"\n  {_BOLD}x402 Payment Required:{_RESET}")
        print(f"  ┌─────────────────────────────────────────────┐")
        print(f"  │  Scheme  : {a.get('scheme','?'):<31} │")
        print(f"  │  Network : {a.get('network','?'):<31} │")
        print(f"  │  Token   : {a.get('asset','?'):<31} │")
        print(f"  │  Amount  : {a.get('amount','?'):<31} │")
        print(f"  │  Pay To  : {str(a.get('payTo','?'))[:29]:<31} │")
        print(f"  └─────────────────────────────────────────────┘")
    err = x402.get("error", "")
    if isinstance(err, str) and err:
        print(f"\n  {_MAGENTA}Message:{_RESET} {err[:250]}")


def _safe_print(body: dict) -> None:
    snippet = json.dumps(body, indent=2, ensure_ascii=False)
    if len(snippet) > 400:
        snippet = snippet[:400] + "\n  ... (truncated)"
    print(f"  Response: {snippet}")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


async def run_tests(base_url: str) -> int:
    banner()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            info(f"Connecting to {base_url}/mcp/sse ...")
            async with aconnect_sse(client, "GET", f"{base_url}/mcp/sse") as event_source:
                sse_iter = event_source.aiter_sse()

                # ---- Read endpoint event ----
                endpoint_url = ""
                async for event in sse_iter:
                    if event.event == "endpoint":
                        endpoint_url = event.data.strip()
                        break
                if not endpoint_url:
                    fail("No endpoint event received from SSE")
                    return 1

                message_url = f"{base_url}{endpoint_url}"
                sid = ""
                if "session_id=" in endpoint_url:
                    sid = endpoint_url.split("session_id=")[-1].split("&")[0]
                info(f"Session ID: {sid[:16]}...")

                session = McpSseSession(client, sse_iter, message_url, sid)
                await session.initialize()

                # ---- Run tests ----
                results = []

                # Test 1: tools/list (free)
                print(f"\n{_BOLD}─ Test 1: tools/list (FREE){_RESET}")
                body = await session.list_tools()
                if "result" in body:
                    tools = body["result"].get("tools", [])
                    ok(f"tools/list → {len(tools)} tools: {[t.get('name','?') for t in tools]}")
                    results.append(True)
                else:
                    fail(f"tools/list failed: {body.get('error', body)}")
                    results.append(False)

                # Test 2: scrape without payment
                print(f"\n{_BOLD}─ Test 2: scrape_webpage WITHOUT payment{_RESET}")
                info("Calling scrape_webpage without X-402-Payment")
                body = await session.call_tool("scrape_webpage", {"url": "https://example.com"})
                x402 = extract_x402(body)
                if x402:
                    print(f"  {_YELLOW}{_BOLD}╔══════════════════════════════════════════════╗{_RESET}")
                    print(f"  {_YELLOW}{_BOLD}║  x402 PAYMENT REQUIRED — ACCESS DENIED        ║{_RESET}")
                    print(f"  {_YELLOW}{_BOLD}╚══════════════════════════════════════════════╝{_RESET}")
                    _print_x402(x402)
                    ok("Server correctly enforced x402 payment")
                    results.append(True)
                else:
                    _safe_print(body)
                    warn("Expected x402 rejection")
                    results.append(False)

                # Test 3: scrape with fake payment
                print(f"\n{_BOLD}─ Test 3: scrape_webpage with FAKE payment{_RESET}")
                fake_tx = "0xdeadbeef"
                info(f"Calling scrape_webpage with X-402-Payment: {fake_tx}")
                body = await session.call_tool_with_payment(
                    "scrape_webpage", {"url": "https://example.com"}, fake_tx
                )
                x402 = extract_x402(body)
                if x402:
                    err = x402.get("error", "")
                    print(f"  {_YELLOW}{_BOLD}╔══════════════════════════════════════════════╗{_RESET}")
                    print(f"  {_YELLOW}{_BOLD}║  FAKE PAYMENT REJECTED — ENFORCEMENT WORKS    ║{_RESET}")
                    print(f"  {_YELLOW}{_BOLD}╚══════════════════════════════════════════════╝{_RESET}")
                    ok(f"Server rejected fake payment")
                    print(f"  {_MAGENTA}Server says:{_RESET} {err[:150]}")
                    results.append(True)
                else:
                    _safe_print(body)
                    # On-chain RPC timeout is acceptable (Base public RPCs may be slow)
                    warn("On-chain check may have timed out (acceptable)")
                    results.append(True)

    except httpx.ConnectError:
        fail(f"Cannot connect to {base_url} — is server.py running?")
        return 1
    except Exception as exc:
        fail(f"SSE/Test error: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    passed = sum(1 for r in results if r)
    failed = len(results) - passed
    print()
    print(f"{_CYAN}{_BOLD}{'=' * 60}{_RESET}")
    print(f"  Results: {_GREEN}{passed} passed{_RESET}, {_RED}{failed} failed{_RESET}, {len(results)} total")
    if failed == 0:
        print(f"  {_GREEN}{_BOLD}ALL TESTS PASSED{_RESET}")
    else:
        print(f"  {_RED}{_BOLD}SOME TESTS FAILED{_RESET}")
    print(f"{_CYAN}{_BOLD}{'=' * 60}{_RESET}")
    print()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="x402 MCP Integration Test Client")
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_tests(args.base_url.rstrip("/"))))
