# Security

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability")
rather than a public issue. Include steps to reproduce and the impact.

Examples of what to report:
- A skill instruction that could make Claude place, modify or cancel an order without explicit
  per-order user confirmation
- A script that executes untrusted input, or leaks credentials or account data
- Guidance that exposes API secrets, TOTP seeds or access tokens

## Principles this project follows

- Analysis never triggers orders. Every order needs an explicit "yes" on a full order ticket
  (see `skills/broker-integration/SKILL.md`).
- The skills never ask users to paste API secrets or TOTP seeds into chat. Self-hosted broker
  servers read credentials from environment variables.
- Scripts are standard-library only and make no network calls, except the optional yfinance fetch
  that the user requests.

## For users

- Prefer read-only broker connections (Kite hosted, Upstox, Fyers) when you only need analysis.
- Never commit `.env` files, API keys, contract notes or tradebooks with personal details.
- Audit community MCP servers before giving them broker credentials.
