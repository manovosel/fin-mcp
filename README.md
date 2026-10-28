# fin-mcp

MCP server for FIN. Six tools, all forwarded to FIN's API with one API token:

- `get_history(from_month, to_month)`
- `get_recommendations(year, month)`
- `get_transactions(from_month, to_month, envelope_id=None)`
- `get_transfers(from_month, to_month)`
- `get_accounts()`
- `set_plan_amounts(year, month, amounts)`

All except `set_plan_amounts` only read. `set_plan_amounts` is the only write: it sets the
Planned amount of envelopes for an existing month. It cannot create months,
cannot change a status, and FIN rejects rows that are already Funded or
Skipped. No calculation, no filtering, no data logged.

## Setup

1. On the FIN server create the API token: `sudo php api.php create-api-token`.
   It is printed once; running the command again revokes the old token. The
   token allows the read actions and the plan-amount write, nothing else.
2. Copy `env.example` to a file outside any web root (mode 600) and fill it in.
3. Create a virtual environment and install `requirements.txt`.
4. Copy `fin-mcp.service.example` to `/etc/systemd/system/fin-mcp.service`,
   adjust the user and paths, then enable and start it.
5. Put a TLS reverse proxy for `PUBLIC_HOSTNAME` in front of `HOST:PORT`.
6. Access tokens are validated against the JWKS of `OIDC_ISSUER`, with audience
   `https://<PUBLIC_HOSTNAME>/mcp`. Register a public OIDC client (PKCE S256)
   with redirect URI `https://claude.ai/api/mcp/auth_callback`.
7. In Claude add a custom connector with URL `https://<PUBLIC_HOSTNAME>/mcp`,
   your client ID and an empty secret. After any change to the tool list,
   remove and re-add the connector.

## License

AGPL-3.0-or-later. Copyright (C) 2026 Marijo Novosel.

The full license text is in [LICENSE](LICENSE).
