# Broker setup notes

Checked Oct 2026 against Zerodha's kite-mcp-server README and a 13 Oct 2026 roundup of broker MCP
pages. Endpoints and capabilities change, so re-check the broker's docs when something fails.

## Claude Code — user scope (all projects)

```bash
claude mcp add --scope user --transport http groww https://mcp.groww.in/mcp
claude mcp list            # verify
# inside Claude Code: /mcp → select server → Authenticate (opens browser)
```

## Claude Code — project `.mcp.json`

```json
{
  "mcpServers": {
    "groww":  { "type": "http", "url": "https://mcp.groww.in/mcp" },
    "kite":   { "type": "http", "url": "https://mcp.kite.trade/mcp" },
    "dhan":   { "type": "http", "url": "https://mcp.dhan.co/mcp" },
    "upstox": { "type": "http", "url": "https://mcp.upstox.com/mcp" },
    "fyers":  { "type": "http", "url": "https://mcp.fyers.in/mcp" }
  }
}
```

## Claude Desktop via mcp-remote (older clients)

```json
{
  "mcpServers": {
    "kite":  { "command": "npx", "args": ["mcp-remote", "https://mcp.kite.trade/mcp"] },
    "groww": { "command": "npx", "args": ["mcp-remote", "https://mcp.groww.in/mcp"] }
  }
}
```

## Per-broker notes

### Groww
- Largest broker by NSE active clients (about 1.34 crore, 29% share, Sep 2026).
- Hosted MCP with OAuth. The session lasts a day, so re-authenticate each morning.
- Orders: buy, limit and stop-loss supported. **Selling holdings needs DDPI** (enable it in the
  Groww app). Cancel support was not documented when checked.
- Charges: equity ₹20 or 0.1% (lower) per order, min ₹5; F&O ₹20 per order (see `trading-costs`).

### Zerodha (Kite)
- Hosted MCP is free with Kite login and 2FA per session. The hosted server excludes regular order
  placement but allows GTT orders.
- Self-host for full tools: `github.com/zerodha/kite-mcp-server` with your Kite Connect API key and
  secret. Use `EXCLUDED_TOOLS` to make a read-only instance.
- 22 tools: login, get_quotes, get_ltp, get_ohlc, get_historical_data, search_instruments,
  get_profile, get_margins, get_holdings, get_positions, get_mf_holdings, place_order, modify_order,
  cancel_order, get_orders, get_trades, get_order_history, get_order_trades, get_gtts,
  place_gtt_order, modify_gtt_order, delete_gtt_order.

### Dhan
- Official hosted MCP with OAuth. Can place, modify and cancel orders, including Super Orders
  (entry + target + stop).
- Live quotes and option chains need the Data API subscription (₹499 + tax a month).

### Upstox
- Official MCP is read-only. The separate Upstox Agent Skill uses the Python SDK with a daily access
  token and can trade, with confirmations and LIMIT orders by default.

### Fyers / Kotak Neo
- Read-only MCPs (Fyers can manage alerts and watchlists). Kotak's works in desktop clients via QR login.

### Angel One
- No official MCP. SmartAPI (REST and WebSocket) is the official route. Community MCP servers exist;
  audit their code before giving them credentials.

## Common order rejections and what they mean

| Message (paraphrased) | Cause | Fix |
|---|---|---|
| Insufficient margin / funds | Margin includes the 50% cash rule for F&O | Add cash or reduce quantity |
| Price outside circuit / DPR | Limit price beyond the band or dynamic range | Re-price inside the band |
| DDPI / TPIN required | Selling from demat without authorisation | Enable DDPI or complete eDIS |
| Quantity freeze limit | F&O order too large for one order | Split (iceberg) |
| Security in ban period | F&O ban: only reducing positions allowed | Reduce, or trade the cash stock |
| MIS not allowed | Surveillance stock, trade-for-trade, or square-off window | Use CNC or skip |
| Session expired | Daily token expiry | Re-authenticate |
