#!/usr/bin/env python3
"""
Web Scraper MCP Service with x402 Cryptocurrency Micropayments
================================================================
Architecture:
  - FastAPI serves HTTP (health, billing info)
  - FastMCP (SSE mode) serves AI agent tools
  - contextvars bridges HTTP payment headers -> MCP tool layer
  - Per-tool x402 verification (NOT global middleware) lets bots
    discover tools for free; payment only on tool invocation.

Stack:
  - FastAPI + Uvicorn      (HTTP server)
  - MCP Python SDK         (FastMCP with SSE transport)
  - x402                   (payment protocol models)
  - httpx                  (async HTTP, non-blocking)
  - BeautifulSoup4         (HTML parsing, html.parser only)

Author: Claude Code AI Assistant
Date:   2026-06-26
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp, Scope, Receive, Send
from x402 import PaymentRequired, PaymentRequirements, ResourceInfo

# =============================================================================
# Application Constants
# =============================================================================

# ---- x402 Payment Configuration ----
RECIPIENT_ADDRESS: str = "0xcf15b97a41022427f50d4bb284c108eb0a716c2b"
PRICE_USD: float = 0.10
USDC_DECIMALS: int = 6
USDC_AMOUNT: int = int(PRICE_USD * (10**USDC_DECIMALS))  # 100000 micro-USDC
CHAIN_ID: int = 8453  # Base Mainnet
TOKEN_SYMBOL: str = "USDC"
USDC_BASE_ADDRESS: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# USDC Transfer event topic (keccak256("Transfer(address,address,uint256)"))
USDC_TRANSFER_TOPIC: str = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# Public Base RPC endpoints (tried in order)
BASE_RPC_URLS: list[str] = [
    os.environ.get("BASE_RPC_URL", ""),
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base-rpc.publicnode.com",
    "https://1rpc.io/base",
]

# ---- Web Scraping Configuration ----
SCRAPE_TIMEOUT: float = 30.0
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Tags completely removed from HTML
REMOVE_TAGS: list[str] = ["script", "style", "noscript", "nav", "footer"]

# Element class/id keywords that trigger removal (case-insensitive)
GARBAGE_KEYWORDS: list[str] = [
    "ad", "sidebar", "popup", "advertisement", "banner",
    "advert", "sponsor", "promo",
]

# ---- File Paths ----
CONFIG_FILE: Path = Path(__file__).parent / "mcp_config.json"
ACCESS_LOG: Path = Path(__file__).parent / "access.log"

# ---- Access Logger ----
_access_logger = logging.getLogger("x402_access")
_access_logger.setLevel(logging.INFO)
_access_fh = logging.FileHandler(str(ACCESS_LOG), encoding="utf-8")
_access_fh.setLevel(logging.INFO)
_access_fmt = logging.Formatter(
    "%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_access_fh.setFormatter(_access_fmt)
# Prevent propagation to root logger (avoids double-printing to console)
_access_logger.propagate = False
# Idempotent: only add the handler once per process
if not _access_logger.handlers:
    _access_logger.addHandler(_access_fh)


def _log_access(
    *,
    payment: str,
    method: str,
    path: str,
    client: str,
    status_code: int,
    detail: str = "",
) -> None:
    """Write one line to access.log. Use structured logfmt-style key=value pairs."""
    _access_logger.info(
        f"payment={payment} "
        f"method={method} "
        f"path={path} "
        f"client={client} "
        f"status={status_code}"
        f"{' detail=' + detail if detail else ''}"
    )


# =============================================================================
# Context Variables — bridge HTTP request context → MCP tool layer
# =============================================================================

_payment_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("x402_payment_ctx", default=None)
)


def get_payment_context() -> Optional[Dict[str, Any]]:
    """Read the payment context set by FastAPI middleware."""
    return _payment_ctx.get()


# =============================================================================
# Web Scraping Engine (fully async)
# =============================================================================


def _build_soup(html: str) -> BeautifulSoup:
    """Parse HTML with html.parser ONLY (no lxml — Windows compat)."""
    return BeautifulSoup(html, "html.parser")


def _remove_junk(soup: BeautifulSoup) -> None:
    """Mutate `soup` in-place to strip junk tags and garbage layers."""
    # Remove by tag name
    for tag in REMOVE_TAGS:
        for el in soup.find_all(tag):
            el.decompose()

    # Remove by class/id keyword match
    for kw in GARBAGE_KEYWORDS:
        pat = re.compile(rf"(?:^|\b|\s|-|_){re.escape(kw)}(?:\b|\s|-|_|$)", re.I)
        for el in soup.find_all(class_=pat):
            el.decompose()
        for el in soup.find_all(id=pat):
            el.decompose()


def _compress_text(text: str) -> str:
    """Collapse whitespace and blank lines."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    return text.strip()


def clean_html(html: str) -> Tuple[str, float]:
    """
    Remove junk from HTML and return (clean_text, garbage_percent).

    garbage_percent = garbage_chars / original_chars * 100, 1 decimal place.
    """
    original_len = len(html)
    if original_len == 0:
        return "", 0.0

    soup = _build_soup(html)
    _remove_junk(soup)
    clean = _compress_text(soup.get_text())

    removed = original_len - len(clean)
    pct = (removed / original_len) * 100.0
    return clean, round(pct, 1)


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for an LLM.

    Rules:
      - CJK characters   → 1 token each
      - Everything else  → 1 token per 4 characters
    """
    cjk = 0
    for ch in text:
        cp = ord(ch)
        if (
            (0x4E00 <= cp <= 0x9FFF)   # CJK Unified
            or (0x3400 <= cp <= 0x4DBF)  # CJK Extension A
            or (0xF900 <= cp <= 0xFAFF)  # CJK Compatibility
            or (0x2E80 <= cp <= 0x2FDF)  # CJK Radicals Supplement
            or (0x3000 <= cp <= 0x303F)  # CJK Symbols/Punctuation (Chinese commas etc.)
        ):
            cjk += 1
    non_cjk = len(text) - cjk
    return cjk + max(0, non_cjk // 4)


def _detect_site_type(soup: BeautifulSoup) -> str:
    """
    Infer site type from <title> and meta keywords/description.

    Returns one of: "article", "product", "social", "other".
    """
    title = (soup.title.string or "") if soup.title else ""

    meta_kw = ""
    meta_desc = ""
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").lower()
        content = meta.get("content", "")
        if name == "keywords":
            meta_kw = content
        elif name == "description":
            meta_desc = content

    combined = f"{title} {meta_kw} {meta_desc}".lower()

    # Pattern dictionaries
    SOCIAL: list[str] = [
        "facebook", "twitter", "instagram", "tiktok", "linkedin",
        "social", "profile", "followers", "friends", "timeline",
        "news feed", "retweet", "hashtag",
    ]
    PRODUCT: list[str] = [
        "shop", "buy", "price", "cart", "product", "checkout",
        "sale", "discount", "amazon", "ebay", "store", "shopify",
        "add to cart", "order", "shipping", "in stock", "sku",
    ]
    ARTICLE: list[str] = [
        "article", "blog", "news", "author", "publish",
        "category", "archive", "opinion", "editorial", "magazine",
        "read more", "byline", "dateline",
    ]

    scores = {
        "social": sum(1 for p in SOCIAL if p in combined),
        "product": sum(1 for p in PRODUCT if p in combined),
        "article": sum(1 for p in ARTICLE if p in combined),
    }

    best = max(scores, key=lambda k: scores[k])  # type: ignore[arg-type]
    if scores[best] == 0:
        return "other"
    return best


async def scrape_url(url: str) -> Dict[str, Any]:
    """
    Core async scraping routine.

    Flow:
      1. HTTP GET via httpx (async, timeout 30 s, follows redirects)
      2. Parse HTML with html.parser
      3. Detect site type BEFORE cleaning
      4. Remove junk & extract clean text
      5. Calculate garbage % and token savings
    """
    async with httpx.AsyncClient(
        timeout=SCRAPE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    soup = _build_soup(html)
    site_type = _detect_site_type(soup)

    clean_text, garbage_pct = clean_html(html)

    tokens_before = estimate_tokens(html)
    tokens_after = estimate_tokens(clean_text)
    tokens_saved = max(0, tokens_before - tokens_after)

    return {
        "raw_url": url,
        "site_type": site_type,
        "clean_core_text": clean_text,
        "garbage_removed_percent": garbage_pct,
        "estimated_token_saved": tokens_saved,
    }


# =============================================================================
# On-Chain Payment Verification (Base / USDC)
# =============================================================================


async def _rpc_call(rpc_url: str, method: str, params: list[Any]) -> Dict[str, Any]:
    """Make a single JSON-RPC call to an Ethereum node."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def verify_usdc_transfer(tx_hash: str) -> Dict[str, Any]:
    """
    Verify that *tx_hash* on Base mainnet transfers ≥ USDC_AMOUNT
    of USDC to RECIPIENT_ADDRESS.

    Returns:
        {"valid": True, "amount": <int>, "recipient": "<addr>"}
        {"valid": False, "reason": "<why>"}
    """
    for rpc_url in BASE_RPC_URLS:
        if not rpc_url:
            continue
        try:
            # 1) Fetch receipt
            receipt_data = await _rpc_call(
                rpc_url, "eth_getTransactionReceipt", [tx_hash]
            )
            receipt = receipt_data.get("result")
            if not receipt:
                return {"valid": False, "reason": "Transaction not found or not yet indexed"}

            if receipt.get("status") != "0x1":
                return {"valid": False, "reason": "Transaction reverted / failed"}

            # 2) Scan logs for USDC Transfer to our address
            logs = receipt.get("logs", [])
            for log in logs:
                addr = log.get("address", "").lower()
                topics = log.get("topics", [])

                # Must be USDC contract + Transfer event
                if addr != USDC_BASE_ADDRESS.lower():
                    continue
                if len(topics) < 3 or topics[0] != USDC_TRANSFER_TOPIC:
                    continue

                # topics[2] = indexed "to" address (padded to 32 bytes)
                to_addr = "0x" + topics[2][-40:]
                amount = int(log.get("data", "0x0"), 16)

                if to_addr.lower() == RECIPIENT_ADDRESS.lower() and amount >= USDC_AMOUNT:
                    return {
                        "valid": True,
                        "amount": str(amount),
                        "recipient": to_addr,
                        "tx_hash": tx_hash,
                    }

            return {"valid": False, "reason": "No matching USDC transfer to recipient"}

        except httpx.HTTPError as exc:
            # Try next RPC
            continue
        except Exception:
            continue

    return {
        "valid": False,
        "reason": (
            "Cannot reach any Base RPC endpoint. "
            "Set BASE_RPC_URL environment variable to a working endpoint."
        ),
    }


# =============================================================================
# x402 Payment Verification (tool-level, NOT global middleware)
# =============================================================================


def build_payment_required_error() -> PaymentRequired:
    """Construct the x402 PaymentRequired response payload."""
    return PaymentRequired(
        x402_version=1,
        error=(
            f"Payment required: {PRICE_USD} {TOKEN_SYMBOL} on Base. "
            f"Send {PRICE_USD} {TOKEN_SYMBOL} to {RECIPIENT_ADDRESS} "
            f"and retry with header X-402-Payment: <tx-hash>"
        ),
        resource=ResourceInfo(
            url="/mcp/messages",
            description="Web scraping tool — per-call micropayment",
        ),
        accepts=[
            PaymentRequirements(
                scheme="usdc",
                network="base",
                amount=str(USDC_AMOUNT),
                pay_to=RECIPIENT_ADDRESS,
                max_timeout_seconds=300,
                asset=TOKEN_SYMBOL,
                extra={},
            )
        ],
    )


async def verify_payment_or_raise() -> None:
    """
    Check the x402 payment proof stored in contextvars.

    Raises:
        PaymentRequiredError — when payment is missing or invalid.
        The error includes structured x402 `PaymentRequired` details.

    On success, returns None (payment is valid).
    """
    ctx = get_payment_context()

    tx_hash: Optional[str] = None
    if ctx:
        tx_hash = ctx.get("x402_payment")

    # ---- No payment proof at all ----
    if not tx_hash:
        payment_required = build_payment_required_error()
        raise PaymentRequiredError(payment_required)

    # ---- Validate tx hash format ----
    if not re.match(r"^0x[a-fA-F0-9]{64}$", tx_hash):
        payment_required = build_payment_required_error()
        payment_required.error = (
            f"Invalid transaction hash format: {tx_hash[:20]}... "
            "Expected 0x + 64 hex characters."
        )
        raise PaymentRequiredError(payment_required)

    # ---- On-chain verification ----
    result = await verify_usdc_transfer(tx_hash)
    if not result.get("valid"):
        payment_required = build_payment_required_error()
        payment_required.error = (
            f"Payment verification failed: {result.get('reason', 'Unknown error')}. "
            f"TX: {tx_hash[:20]}..."
        )
        raise PaymentRequiredError(payment_required)

    # Payment is valid — proceed silently


class PaymentRequiredError(Exception):
    """Wraps an x402 `PaymentRequired` payload as a Python exception.

    When raised inside a FastMCP tool, FastMCP serializes the error message
    and returns it as an MCP JSON-RPC error.  The MCP client (AI agent) can
    parse the JSON payload to understand what payment is required.
    """

    def __init__(self, payment_required: PaymentRequired) -> None:
        self.payment_required = payment_required
        # Serialize the x402 payload as a JSON string — the MCP client
        # (or its x402 adapter) can deserialize and act on it.
        super().__init__(payment_required.model_dump_json())


# =============================================================================
# FastMCP Server — AI Agent Tools
# =============================================================================

mcp = FastMCP(
    "web-scraper-x402",
    instructions=(
        "Web scraping service with x402 micropayments.\n"
        f"• List tools: FREE (no payment needed)\n"
        f"• Call `scrape_webpage`: {PRICE_USD} USDC on Base per call\n"
        f"• Payment address: {RECIPIENT_ADDRESS}\n"
        "• Add header `X-402-Payment: <tx-hash>` with your tool call request\n"
        "• Call `get_service_info` for full pricing details (free)"
    ),
)


@mcp.tool(
    name="scrape_webpage",
    description=(
        "Scrape and clean a webpage. Removes scripts, styles, navigation, "
        "ads, and other junk. Returns clean text with statistics.\n\n"
        f"**COST: {PRICE_USD} USDC on Base chain per call.**\n"
        f"Send {PRICE_USD} USDC to `{RECIPIENT_ADDRESS}` and include "
        "header `X-402-Payment: <your-tx-hash>` when calling this tool.\n"
        "Payment is verified on-chain before scraping proceeds."
    ),
)
async def scrape_webpage(url: str) -> Dict[str, Any]:
    """
    Scrape a webpage and return cleaned text + statistics.

    Payment required: 0.10 USDC on Base chain (x402 protocol).
    Include header `X-402-Payment: <tx-hash>` with the tool call.

    Args:
        url: The full URL of the webpage to scrape (e.g. https://example.com)
    """
    # ---- PER-TOOL x402 PAYMENT CHECK (not global middleware!) ----
    await verify_payment_or_raise()

    # ---- Execute scraping ----
    return await scrape_url(url)


@mcp.tool(
    name="get_service_info",
    description=(
        "Get service pricing, payment instructions, and supported features. "
        "This tool is FREE to call — no payment required."
    ),
)
async def get_service_info() -> Dict[str, Any]:
    """Return service metadata. Always free."""
    return {
        "service": "Web Scraper MCP with x402 Micropayments",
        "version": "1.0.0",
        "price_per_call": f"{PRICE_USD} USD",
        "accepted_token": TOKEN_SYMBOL,
        "token_contract": USDC_BASE_ADDRESS,
        "chain": "Base",
        "chain_id": CHAIN_ID,
        "recipient_address": RECIPIENT_ADDRESS,
        "amount_micro_usdc": str(USDC_AMOUNT),
        "amount_display": f"{PRICE_USD} USDC",
        "free_tools": ["get_service_info"],
        "paid_tools": ["scrape_webpage"],
        "features": {
            "html_cleaning": [
                "Remove <script>, <style>, <noscript>, <nav>, <footer>",
                "Remove elements with ad/sidebar/popup classes/ids",
            ],
            "output_format": {
                "raw_url": "Original URL",
                "site_type": "article | product | social | other",
                "clean_core_text": "Cleaned plain text",
                "garbage_removed_percent": "Percentage of junk removed",
                "estimated_token_saved": "Estimated LLM tokens saved",
            },
        },
    }


# =============================================================================
# Config Generator
# =============================================================================


def generate_mcp_config() -> None:
    """Create mcp_config.json on first run if it doesn't exist."""
    if CONFIG_FILE.exists():
        print(f"[config] {CONFIG_FILE} already exists — skipping")
        return

    config: Dict[str, Any] = {
        "mcpServers": {
            "web-scraper-x402": {
                "url": "http://localhost:8000/mcp/sse",
                "transport": "sse",
                "description": (
                    "Web scraper with x402 micropayments "
                    f"({PRICE_USD} USDC/call on Base)"
                ),
                "payment": {
                    "protocol": "x402",
                    "network": "base",
                    "chain_id": CHAIN_ID,
                    "token": TOKEN_SYMBOL,
                    "token_address": USDC_BASE_ADDRESS,
                    "amount_micro_usdc": str(USDC_AMOUNT),
                    "amount_display": f"{PRICE_USD} USDC",
                    "recipient": RECIPIENT_ADDRESS,
                    "header": "X-402-Payment",
                },
                "env": {
                    "BASE_RPC_URL": {
                        "description": "Optional — custom Base RPC endpoint",
                        "default": "https://mainnet.base.org",
                    }
                },
            }
        }
    }

    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[config] Created {CONFIG_FILE}")


# =============================================================================
# FastAPI Application
# =============================================================================

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup + shutdown lifecycle (replaces deprecated on_event)."""
    # ---- STARTUP ----
    generate_mcp_config()
    print("=" * 60)
    print("  Web Scraper MCP Service with x402 Payments")
    print("=" * 60)
    print(f"  HTTP API     : http://localhost:8000")
    print(f"  API Docs     : http://localhost:8000/docs")
    print(f"  MCP SSE      : http://localhost:8000/mcp/sse")
    print(f"  Health       : http://localhost:8000/health")
    print(f"  Payment Info : http://localhost:8000/payment-info")
    print("-" * 60)
    print(f"  Price        : {PRICE_USD} USD ({TOKEN_SYMBOL} on Base)")
    print(f"  Recipient    : {RECIPIENT_ADDRESS}")
    print(f"  USDC Contract: {USDC_BASE_ADDRESS}")
    print(f"  Access Log   : {ACCESS_LOG}")
    print(f"  Amount       : {USDC_AMOUNT} (micro-USDC)")
    print("=" * 60)
    yield
    # ---- SHUTDOWN ----
    print("[shutdown] Web Scraper MCP Service stopped.")


app = FastAPI(
    title="Web Scraper MCP Service",
    description=(
        "MCP server for AI agents to scrape and clean webpages. "
        f"Powered by x402: {PRICE_USD} USDC per call on Base chain."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


class _X402ContextMiddleware:
    """
    Raw ASGI middleware — extracts X-402-Payment header into contextvars
    and logs every request to access.log.

    Uses a raw ASGI `__call__` rather than Starlette's BaseHTTPMiddleware
    so that streaming responses (SSE events on /mcp/sse) pass through
    without interference.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ---- Extract headers from raw ASGI scope ----
        raw_headers = dict(scope.get("headers", []))
        payment_proof = (
            raw_headers.get(b"x-402-payment", b"")
            or raw_headers.get(b"X-402-Payment", b"")
        ).decode("latin-1") or None

        ctx_data: Dict[str, Any] = {
            "x402_payment": payment_proof,
            "user_agent": raw_headers.get(b"user-agent", b"").decode("latin-1", errors="replace"),
            "content_type": raw_headers.get(b"content-type", b"").decode("latin-1", errors="replace"),
            "path": scope.get("path", "/"),
            "method": scope.get("method", "?"),
            "client": (scope.get("client") or ("unknown", 0))[0],
        }

        token = _payment_ctx.set(ctx_data)

        # ---- Capture the HTTP status code from the response start message ----
        captured_status: list[int] = [200]

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                captured_status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            _payment_ctx.reset(token)
            # ---- Write access log ----
            _log_access(
                payment=payment_proof if payment_proof else "none",
                method=ctx_data["method"],
                path=ctx_data["path"],
                client=ctx_data["client"],
                status_code=captured_status[0],
            )


# Register the raw ASGI middleware (before FastAPI's own middleware stack)
app.add_middleware(_X402ContextMiddleware)


# ---------------------------------------------------------------------------
# Health & Info endpoints (free — no payment needed)
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    """Service root — free to access."""
    return {
        "service": "Web Scraper MCP with x402",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "mcp_sse": "/mcp/sse",
    }


@app.get("/health")
async def health():
    """Health check — free."""
    return {"status": "ok"}


@app.get("/payment-info")
async def payment_info():
    """Return x402 payment requirements (free)."""
    return {
        "protocol": "x402",
        "version": 1,
        "price": f"{PRICE_USD} USD",
        "token": TOKEN_SYMBOL,
        "token_address": USDC_BASE_ADDRESS,
        "network": "Base",
        "chain_id": CHAIN_ID,
        "recipient": RECIPIENT_ADDRESS,
        "amount_micro_usdc": str(USDC_AMOUNT),
        "payment_header": "X-402-Payment",
        "example": (
            f"Send {PRICE_USD} {TOKEN_SYMBOL} to "
            f"{RECIPIENT_ADDRESS} on Base chain, then include "
            f"header 'X-402-Payment: <your-tx-hash>' when calling "
            f"the scrape_webpage MCP tool."
        ),
    }


# ---------------------------------------------------------------------------
# Mount FastMCP SSE app on /mcp
# ---------------------------------------------------------------------------

# FastMCP's sse_app() returns a Starlette ASGI app.
# Mounting it on /mcp creates:
#   GET  /mcp/sse       — SSE connection
#   POST /mcp/messages  — JSON-RPC messages (tool calls)
#
# The FastAPI middleware (extract_x402_headers) runs BEFORE the mounted
# app processes the request, so contextvars are set correctly for tool calls.

mcp_sse_app = mcp.sse_app()
app.mount("/mcp", mcp_sse_app)


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    # Ensure config exists before starting
    generate_mcp_config()

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        reload=False,
    )
