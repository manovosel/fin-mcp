"""
MCP server for FIN. Reads history, funding recommendations, individual
transactions, transfers and account balances through FIN's API, and can set
Planned envelope amounts for existing months. It never changes a plan's status
and can do nothing else.
Everything goes through one API token; FIN's JSON is returned unchanged: no
calculation, no filtering, no logging of data.
"""

import asyncio
import logging
import os

import httpx
import jwt
import uvicorn
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from typing_extensions import TypedDict

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

FIN_URL = os.environ.get("FIN_URL", "").rstrip("/")
FIN_TOKEN = os.environ.get("FIN_TOKEN")
PUBLIC_HOSTNAME = os.environ.get("PUBLIC_HOSTNAME")
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
PORT = int(os.environ.get("PORT", "3940"))
HOST = os.environ.get("HOST", "0.0.0.0")

if not FIN_URL or not FIN_TOKEN:
    raise SystemExit("FIN_URL and FIN_TOKEN are required.")
if not PUBLIC_HOSTNAME:
    raise SystemExit("PUBLIC_HOSTNAME is required.")
if not OIDC_ISSUER or not OIDC_CLIENT_ID:
    raise SystemExit("OIDC_ISSUER and OIDC_CLIENT_ID are required.")

logger = logging.getLogger(__name__)

RESOURCE_URL = f"https://{PUBLIC_HOSTNAME}/mcp"
_jwks = PyJWKClient(f"{OIDC_ISSUER}/jwks.json")


class OidcVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(_jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=OIDC_ISSUER,
                audience=RESOURCE_URL,
                options={"require": ["exp", "iss", "aud"]},
            )
            return AccessToken(
                token="oidc",
                client_id=OIDC_CLIENT_ID,
                scopes=["openid"],
                expires_at=int(claims["exp"]),
                resource=RESOURCE_URL,
            )
        except Exception as exc:
            logger.warning("Token verification failed (%s)", type(exc).__name__)
            return None


mcp = FastMCP(
    "fin",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[PUBLIC_HOSTNAME, f"{PUBLIC_HOSTNAME}:*"],
        allowed_origins=[f"https://{PUBLIC_HOSTNAME}"],
    ),
    token_verifier=OidcVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(OIDC_ISSUER),
        resource_server_url=AnyHttpUrl(RESOURCE_URL),
        required_scopes=["openid"],
        validate_token_resource=True,
    ),
)


def _result(resp: httpx.Response) -> dict:
    if resp.status_code != 200:
        try:
            message = resp.json().get("error", resp.text[:200])
        except ValueError:
            message = resp.text[:200]
        raise ValueError(f"FIN returned {resp.status_code}: {message}")
    return resp.json()


async def fin(action: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FIN_URL}/api.php",
            params={"action": action, **params},
            headers={"Authorization": f"Bearer {FIN_TOKEN}"},
        )
    return _result(resp)


async def fin_post(action: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{FIN_URL}/api.php",
            params={"action": action},
            json=body,
            headers={"Authorization": f"Bearer {FIN_TOKEN}"},
        )
    return _result(resp)


class PlanAmount(TypedDict):
    envelope_id: int
    amount: float


@mcp.tool()
async def get_history(from_month: str, to_month: str) -> dict:
    """Read-only monthly history from FIN for a range of months, inclusive,
    formatted YYYY-MM (e.g. from_month='2026-04', to_month='2026-10').
    Amounts are in the FIN currency (see the 'currency' field).

    Per month: overview (planned_allocations, actual_expenses) and three role
    tables (daily, reserved, savings), each with rows per envelope
    (envelope_id, envelope, planned, funded, actual, difference, balance) and
    totals. planned = the month's plan (skipped plans excluded); funded =
    money actually moved into the envelope for that plan; actual = net
    spending by spend date (refunds subtract); difference = planned - actual;
    balance = envelope balance at month end (today for the current month; a
    future month shows its plan so far with current balances).

    income = actual Income events on accounts in that calendar month (total
    plus events with date, amount, note). Only classified Income events
    count, so unreconciled statement credits are missing; future-dated events
    are not included. The Month salary / planned income is deliberately not
    part of this data.

    Months still in FIN's database are 'full' (coverage='full'). Older
    months that exist only as spending are 'actual_only': per envelope just
    the actual spending (no planned, funded, difference or balance). A range
    with no data returns an error. The response also has 'savings_goals'
    (target, balance and progress_percent per Savings envelope; no deadlines
    exist).

    The current month (period='current') also has 'safe_to_spend': the
    figure the Today screen shows, with every input (amount, label, as_of,
    daily_total, daily_envelopes with id, name and balance, days_in_month,
    remaining_days, raw_per_day, rounded_amount, rounding, zero_reason).
    Rule: sum the current balances of all Daily envelopes (the balance, not
    the plan; negative balances count with their sign), divide by the days
    left in the month including today (at least 1), round down to a multiple
    of 10, never below 0. It is 0 when the Daily total is negative (label
    'over plan'), exactly 0, or below 10 per remaining day; zero_reason names
    which case. Past and future months have no safe_to_spend. Use it to
    explain why the figure is what it is, for example why it is 0."""
    return await fin("history", **{"from": from_month, "to": to_month})


@mcp.tool()
async def get_recommendations(year: int, month: int) -> dict:
    """FIN's own deterministic funding recommendation per envelope for a
    target month (year, month 1-12). The target month must already exist in
    FIN. Per envelope: role, current_plan and its status, the recommended
    'amount' (null when history is insufficient, and always null for Savings,
    which is goal-driven), confidence, pattern, representative_months, stats,
    per-month observations (past spending, with outlier/exclusion flags), any
    manual baseline and note, and an assessment of the current plan against
    the recommendation. It is based on past spending per envelope, not past
    funding, and ignores income. Use it as a reference, not as the plan.
    The result is very large (about 200k characters)."""
    return await fin("fundingRecommendations", year=year, month=month)


@mcp.tool()
async def get_transactions(
    from_month: str, to_month: str, envelope_id: int | None = None
) -> dict:
    """Read-only list of individual actual transactions from FIN for a range
    of months, inclusive, formatted YYYY-MM, at most 3 months per call.
    Optionally filter to one envelope with envelope_id. Per row: id, date,
    envelope_id, envelope, description, amount (expense positive, refund
    negative), kind (expense or refund) and account (display name; null when
    the payment account is unknown). Selection follows the monthly report:
    calendar month of the spend date, future-dated rows excluded, no
    transfers or adjustments. Use it to find misbooked, duplicate or one-off
    payments behind the monthly totals in get_history."""
    params = {"from": from_month, "to": to_month}
    if envelope_id is not None:
        params["envelope_id"] = envelope_id
    return await fin("transactions", **params)


@mcp.tool()
async def get_transfers(from_month: str, to_month: str) -> dict:
    """Read-only list of transfers from FIN for a range of months, inclusive,
    formatted YYYY-MM, at most 3 months per call. The result has 'from', 'to'
    and 'transfers'. Every row has kind, id, date, amount (positive), note
    and payment_id.

    kind='envelope': money moved from one envelope to another
    (from_envelope, to_envelope). 'undone' is true when the move was undone;
    it is still listed.

    kind='account': money moved between accounts (from_account, to_account,
    display names) with the 'currency' of the amount and
    'linked_to_statement_row' (true when the transfer is linked to an
    imported statement row).

    One real payment can be recorded both as an envelope transfer and as an
    account transfer. Both rows then carry the same payment_id, so group by
    payment_id and count that payment once; never add the two rows up.

    Selection follows the monthly report: calendar month of the transfer
    date, future-dated rows excluded. Expense and refund transactions
    (get_transactions), income (get_history), funding movements, balance
    observations and adjustments are not included. Account numbers are not
    exposed. The amount is the single FIN-currency amount of the transfer,
    not the per-account amount shown by get_accounts."""
    return await fin("transfers", **{"from": from_month, "to": to_month})


@mcp.tool()
async def get_accounts() -> dict:
    """Read-only list of FIN's accounts with their current balance, as of
    today (top-level 'as_of' and 'currency'). Per account: id, name,
    currency, balance, balance_known, status ('active' or 'archived') and
    last_observation_date (the latest balance observation the balance is
    anchored to; null if none). The balance is exactly what the Accounts
    screen shows: expressed in the account's own currency (a Helper-currency
    account reports its Helper-currency amount; check the per-account
    'currency' field before adding balances together), and null with
    balance_known=false when no observation applies; an unknown balance is
    never zero. Archived accounts are included. Account numbers are not
    exposed. Use it to check that the balances entered in FIN look right,
    for example against the envelope balances in get_history."""
    return await fin("accountBalances")


@mcp.tool()
async def set_plan_amounts(year: int, month: int, amounts: list[PlanAmount]) -> dict:
    """WRITES to FIN. Sets the planned amount of envelopes for an existing
    month (year, month 1-12). Call it only after the user has approved the
    exact envelopes and amounts in the conversation, and never on your own
    initiative.

    amounts is a list of {envelope_id, amount}. Amounts are absolute, in the
    one FIN currency, zero or more, at most two decimals. Only the listed
    envelopes change; the others are untouched. An envelope appears at most
    once per call. The call is all-or-nothing: if any row is invalid nothing
    is written and the error names the row.

    Limits enforced by FIN: the month must already exist (this tool cannot
    create months); every amount stays in status Planned and this tool cannot
    change a status; a row that is already Funded or Skipped is rejected; no
    funding, transaction or balance is created or changed.

    The result lists, per envelope, the previous amount (null if there was
    no row) and the new amount. Report these to the user, and verify with
    get_history afterwards."""
    return await fin_post("setPlanAmounts", {"year": year, "month": month, "amounts": amounts})


app = mcp.streamable_http_app()

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)