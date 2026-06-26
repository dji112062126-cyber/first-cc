# Web Scraper MCP Service with x402 Micropayments

**AI-powered web scraping behind a cryptocurrency paywall — pay per use, free to discover.**

A [FastMCP](https://github.com/jlowin/fastmcp) server that lets AI agents scrape and clean webpages. Tools are free to list and inspect, but invoking `scrape_webpage` costs **0.10 USDC** on the **Base** blockchain (L2). Payment is verified on-chain via standard USDC Transfer events before scraping proceeds.

---

## Architecture

```
External AI Agent
       │
       ▼
┌─────────────────┐     ┌──────────────────────┐
│  ngrok / cpolar  │────▶│  FastAPI (port 8000)  │
│  HTTPS tunnel    │     │  ├─ /health           │
└─────────────────┘     │  ├─ /payment-info     │
                        │  └─ /mcp/sse  (FastMCP)│
                        │       └─ /mcp/messages │
                        └──────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │  contextvars bridge      │
                    │  X-402-Payment header    │
                    │  ➜ per-tool x402 check   │
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │  httpx + BeautifulSoup   │
                    │  On-chain USDC verify    │
                    └─────────────────────────┘
```

### Key Design Decisions

| Concern | Approach |
|---|---|
| **Free tool discovery** | `/tools/list` and `get_service_info` require NO payment — bots can browse before paying |
| **Per-tool payment** | Only `scrape_webpage` calls `verify_payment_or_raise()` — NOT global middleware |
| **Header → context** | FastAPI middleware captures `X-402-Payment` header into Python `contextvars`; the MCP tool reads it |
| **Async scraping** | `httpx.AsyncClient` throughout — non-blocking under concurrent load |
| **Windows-safe HTML parsing** | `html.parser` only, no `lxml` (avoids C++ compiler errors on Windows) |

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Python | **3.12+** | Tested with 3.12.9 on Windows 11 |
| fastapi | ≥0.100 | HTTP framework |
| uvicorn | ≥0.30 | ASGI server |
| mcp | ≥1.0 | FastMCP SDK |
| x402 | ≥2.0 | Payment protocol models |
| httpx | ≥0.27 | Async HTTP client |
| beautifulsoup4 | ≥4.12 | HTML parser (html.parser) |

All installable via `pip` — no C/C++ build tools needed.

---

## Quick Start

### 1. Install Dependencies

```powershell
# From the project root (C:\Agent\first-cc)
& "C:\Program Files\Python312\python.exe" -m pip install --user fastapi uvicorn mcp x402 httpx beautifulsoup4
```

If the above fails, try:

```powershell
python -m pip install --user fastapi uvicorn mcp x402 httpx beautifulsoup4
```

### 2. Start the Server

```powershell
& "C:\Program Files\Python312\python.exe" server.py
```

You should see:

```
============================================================
  Web Scraper MCP Service with x402 Payments
============================================================
  HTTP API     : http://localhost:8000
  API Docs     : http://localhost:8000/docs
  MCP SSE      : http://localhost:8000/mcp/sse
  Health       : http://localhost:8000/health
  Payment Info : http://localhost:8000/payment-info
  Access Log   : C:\Agent\first-cc\access.log
------------------------------------------------------------
  Price        : 0.1 USD (USDC on Base)
  Recipient    : 0xcf15b97a41022427f50d4bb284c108eb0a716c2b
  USDC Contract: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
  Amount       : 100000 (micro-USDC)
============================================================
```

### 3. Verify It Works

```powershell
# In another terminal:
curl http://localhost:8000/health
# → {"status":"ok"}

curl http://localhost:8000/payment-info
# → payment details JSON
```

---

## Exposing to the Internet (Tunnel Setup)

External AI agents need a public HTTPS URL. Pick one tunnel provider:

### Option A: ngrok (recommended)

1. **Download & Install**
   - Visit https://ngrok.com/download
   - Download the Windows `.exe` and place it in `C:\ngrok\ngrok.exe`

2. **Sign Up & Get Auth Token**
   - Create a free account at https://dashboard.ngrok.com/signup
   - Copy your authtoken from https://dashboard.ngrok.com/get-started/your-authtoken

3. **Authenticate** (one-time setup)
   ```powershell
   C:\ngrok\ngrok.exe config add-authtoken <YOUR_AUTHTOKEN>
   ```

4. **Start the Tunnel**
   ```powershell
   C:\ngrok\ngrok.exe http 8000
   ```
   You will see a public URL like `https://abc123.ngrok-free.app`.

### Option B: cpolar

1. **Download**
   - Visit https://www.cpolar.com/download
   - Install the Windows `.exe`

2. **Register**
   - Sign up at https://www.cpolar.com
   - Copy your auth token from the dashboard

3. **Authenticate**
   ```powershell
   cpolar authtoken <YOUR_AUTHTOKEN>
   ```

4. **Start the Tunnel**
   ```powershell
   cpolar http 8000
   ```
   You will see a public URL like `https://abc123.cpolar.io`.

### Auto-Sync the Public URL

Once either tunnel is running, sync its URL into `mcp_config.json`:

```powershell
# One-shot sync
& "C:\Program Files\Python312\python.exe" update_config.py

# Or keep watching (auto-syncs every 60s when the tunnel URL changes)
& "C:\Program Files\Python312\python.exe" update_config.py --watch
```

This updates the `url` field inside `mcp_config.json` so your MCP client always points at the live tunnel address.

---

## MCP Client Configuration

Point your MCP client (Claude Desktop, etc.) at `mcp_config.json`:

```json
{
  "mcpServers": {
    "web-scraper-x402": {
      "url": "https://<YOUR_TUNNEL_URL>/mcp/sse",
      "transport": "sse",
      "description": "Web scraper with x402 micropayments (0.1 USDC/call on Base)",
      "payment": {
        "protocol": "x402",
        "network": "base",
        "chain_id": 8453,
        "token": "USDC",
        "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "amount_micro_usdc": "100000",
        "amount_display": "0.1 USDC",
        "recipient": "0xcf15b97a41022427f50d4bb284c108eb0a716c2b",
        "header": "X-402-Payment"
      }
    }
  }
}
```

---

## Payment Flow

```
Agent calls tools/list   ─▶  FREE ✓  (no payment needed)
Agent calls get_service_info ─▶  FREE ✓
Agent calls scrape_webpage ─▶  HTTP 402  ←  x402 PaymentRequired
                                       │
                              "Send 0.1 USDC to 0xcf15b97a41..."
                                       │
                   User sends USDC on Base chain
                                       │
Agent retries with header:
  X-402-Payment: 0x<tx-hash>  ─▶  Server verifies on-chain
                                       │
                              ┌────────┴────────┐
                              │ Valid?           │
                              │  Yes → scrape    │
                              │  No  → 402 again │
                              └─────────────────┘
```

**USDC Contract on Base:** `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`

---

## API Reference

### HTTP Endpoints (all free)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Service root — version, status, doc links |
| `GET` | `/health` | Health check (`{"status":"ok"}`) |
| `GET` | `/payment-info` | Full x402 pricing & payment instructions |
| `GET` | `/docs` | Auto-generated Swagger UI |

### MCP Tools

| Tool | Cost | Description |
|---|---|---|
| `get_service_info` | **FREE** | Pricing, accepted tokens, features |
| `scrape_webpage` | **0.10 USDC** | Scrape & clean a URL. Requires `X-402-Payment` header. |

### `scrape_webpage` Response

```json
{
  "raw_url": "https://example.com",
  "site_type": "article",
  "clean_core_text": "Cleaned plain text content...",
  "garbage_removed_percent": 73.5,
  "estimated_token_saved": 1520
}
```

---

## Logging & Monitoring

### Access Log

Every HTTP request is appended to `access.log` in logfmt-style key=value format:

```
2026-06-26 21:15:03 | payment=0xabcd... method=POST path=/mcp/messages client=127.0.0.1 status=200
2026-06-26 21:15:10 | payment=none method=GET path=/health client=127.0.0.1 status=200
2026-06-26 21:15:15 | payment=none method=POST path=/mcp/messages client=127.0.0.1 status=200
```

### Real-Time Log Viewing (Windows)

```powershell
# PowerShell — like tail -f on Linux
Get-Content C:\Agent\first-cc\access.log -Wait -Tail 20

# Or if you have Git Bash:
tail -f C:/Agent/first-cc/access.log
```

---

## Testing

Run the integration test suite against a running server:

```powershell
# Start the server first (in its own terminal):
& "C:\Program Files\Python312\python.exe" server.py

# Then run the test client:
& "C:\Program Files\Python312\python.exe" test_client.py
```

The test suite:
1. Calls `tools/list` — expects success (free)
2. Calls `scrape_webpage` without payment — expects x402 rejection
3. Calls `scrape_webpage` with a fake tx hash — expects format rejection

All results are printed with colour-coded PASS/FAIL indicators.

---

## Project Files

| File | Purpose |
|---|---|
| `server.py` | Main service — FastAPI + FastMCP + x402 + scraping engine |
| `test_client.py` | Integration test client (httpx-based) |
| `update_config.py` | Auto-detect tunnel URL and sync to mcp_config.json |
| `mcp_config.json` | MCP client configuration (auto-generated) |
| `access.log` | Request audit log (auto-created) |
| `agent_manifest.json` | Coinbase Agent Marketplace listing manifest |
| `README.md` | This file |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Port 8000 already in use | `taskkill /F /IM python.exe` |
| `mcp` not found | `python -m pip install --user mcp` |
| `x402` import error | `python -m pip install --user x402` |
| Tunnel URL not detected | Ensure ngrok/cpolar is running with `http 8000` |
| On-chain verification hangs | Set `BASE_RPC_URL` env var to a paid RPC endpoint |
| `lxml` compile error | Not needed — this project uses `html.parser` only |
