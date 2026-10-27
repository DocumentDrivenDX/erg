"""Read-only Codex billing capture. Secrets never leave this transport's memory.

Only the existing file-backed ChatGPT login is supported. No login, refresh,
purchase, credit consumption, redirects, browser cookies or custom endpoint.
"""

import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import accounting as a

BASE = "https://chatgpt.com/backend-api"
MAX_BODY = 2 * 1024 * 1024
CACHE = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def allowed(path):
    if path in (
        "/wham/usage",
        "/wham/accounts/check",
        "/accounts/check/v4-2023-04-27",
        "/wham/rate-limit-reset-credits",
    ):
        return True
    if path.startswith("/wham/rate-limit-reset-credits/history"):
        u = urllib.parse.urlsplit(path)
        return (
            u.path == "/wham/rate-limit-reset-credits/history"
            and not u.fragment
            and set(urllib.parse.parse_qs(u.query)) <= {"cursor"}
        )
    return (
        path in ("/checkout_pricing_config/configs/USD",)
        or path.startswith("/subscriptions?account_id=")
        and "&" not in path
        and "#" not in path
    )


def read(home, expected_account="", timeout=18):
    """Return sanitized billing documents, never the auth object or HTTP errors."""
    path = Path(home) / "auth.json"
    now = int(time.time())
    try:
        st = path.stat()
        if st.st_size > 65536:
            return dict(ts=now, status="credential_store_unsupported")
        key = (str(path.resolve()), st.st_ino, st.st_mtime_ns, st.st_size, expected_account)
        cached = CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 900:
            return cached[1]
        auth = json.loads(path.read_text())
        tokens = auth.get("tokens") or {}
        token = tokens.get("access_token")
        account = tokens.get("account_id")
        if (
            auth.get("auth_mode") not in (None, "chatgpt")
            or not isinstance(token, str)
            or not isinstance(account, str)
            or not token
            or not account
        ):
            return dict(ts=now, status="native_chatgpt_login_unavailable")
        if expected_account and account != expected_account:
            return dict(ts=now, status="account_mismatch")
    except (OSError, ValueError, TypeError):
        return dict(ts=now, status="native_chatgpt_login_unavailable")
    opener = urllib.request.build_opener(NoRedirect)
    deadline = time.monotonic() + timeout
    statuses = {}
    documents = {}

    def get(name, path):
        if not allowed(path):
            raise ValueError("billing endpoint is not allowlisted")
        if time.monotonic() >= deadline:
            statuses[name] = "deadline"
            return None
        request = urllib.request.Request(
            BASE + path,
            method="GET",
            headers={
                "Authorization": "Bearer " + token,
                "ChatGPT-Account-ID": account,
                "Accept": "application/json",
                "User-Agent": "erg-local-monitor/0.1",
                "originator": "erg_local_monitor",
            },
        )
        try:
            with opener.open(
                request, timeout=min(4, max(0.1, deadline - time.monotonic()))
            ) as response:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    statuses[name] = "response_too_large"
                    return None
                data = json.loads(raw)
                if not isinstance(data, dict):
                    statuses[name] = "invalid_response"
                    return None
                statuses[name] = "ok"
                return data
        except urllib.error.HTTPError as error:
            statuses[name] = "http_" + str(error.code)
        except (OSError, ValueError, TimeoutError):
            statuses[name] = "unavailable"
        return None

    usage = get("usage", "/wham/usage")
    if not usage or usage.get("account_id") != account:
        return dict(
            ts=now, status="account_mismatch" if usage else "usage_unavailable", endpoints=statuses
        )
    # Copy only nonsecret fields; never retain raw service responses.
    documents["account"] = dict(account_id=account, plan_type=a.text(usage.get("plan_type")))
    if isinstance(usage.get("email"), str):
        documents["account"]["email"] = usage["email"]
    documents["rate_limit"] = usage.get("rate_limit") or {}
    documents["credits"] = {
        k: (usage.get("credits") or {}).get(k) for k in ("balance", "has_credits", "unlimited")
    }
    inventory = get("reset_inventory", "/wham/rate-limit-reset-credits")
    if inventory:
        documents["available_count"] = inventory.get("available_count")
    history = []
    cursor = ""
    seen = set()
    for page in range(4):
        response = get(
            "reset_history",
            "/wham/rate-limit-reset-credits/history"
            + ("?cursor=" + urllib.parse.quote(cursor, safe="") if cursor else ""),
        )
        if not response:
            break
        history.extend(
            {k: e.get(k) for k in ("id", "kind", "occurred_at")}
            for e in (response.get("events") or [])[:200]
            if isinstance(e, dict)
        )
        cursor = response.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen or len(cursor) > 2048:
            break
        seen.add(cursor)
    documents["resets"] = history
    entitlement = get("entitlement", "/accounts/check/v4-2023-04-27")
    accounts = (entitlement or {}).get("accounts") or {}
    selected = accounts.get(account) if isinstance(accounts, dict) else None
    if isinstance(selected, dict):
        ent = selected.get("entitlement") or {}
        documents["entitlement"] = {
            k: ent.get(k) for k in ("has_active_subscription", "billing_currency")
        }
    pricing = get("pricing", "/checkout_pricing_config/configs/USD")
    if pricing:
        c = pricing.get("currency_config") or {}
        plan = documents["account"]["plan_type"]
        entry = c.get(plan) or {}
        documents["price"] = dict(
            amount=(entry.get("month") or {}).get("amount"),
            currency=c.get("symbol_code"),
            exponent=c.get("minor_unit_exponent"),
        )
    # Subscription responses may contain payment details. Extract only identifiers
    # and dates once their schema is verified; do not persist arbitrary invoices.
    subscriptions = get(
        "subscription", "/subscriptions?account_id=" + urllib.parse.quote(account, safe="")
    )
    if subscriptions:
        documents["subscription_read_available"] = True
    result = dict(
        ts=now,
        status="complete" if all(v == "ok" for v in statuses.values()) else "partial",
        endpoints=statuses,
        **documents,
    )
    if len(CACHE) >= 128:
        CACHE.clear()
    CACHE[key] = (time.monotonic(), result)
    return result


def timestamp(value):
    try:
        return (
            int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            if isinstance(value, str)
            else None
        )
    except (ValueError, OverflowError):
        return None


def events(home, result):
    if not result or not result.get("account"):
        return []
    ts = result["ts"]
    fields = a.identity_fields(result["account"], "openai")
    installation = a.digest("codex", str(home))
    base = dict(
        ts=ts,
        tool="codex",
        session="",
        installation=installation,
        provider="openai",
        account_key=fields["account_key"],
        subscription_key=fields["subscription_key"],
        plan=fields["plan"],
    )
    identity = dict(
        t="identity",
        uid=a.digest("codex-billing", installation, ts),
        ts=ts,
        tool="codex",
        session="",
        installation=installation,
        provider="openai",
        provenance="codex_billing_api",
        scope="account_plan",
        billing_status=json.dumps(
            result.get("endpoints", {}), sort_keys=True, separators=(",", ":")
        ),
        **fields,
    )
    price = result.get("price") or {}
    amount = a.number(price.get("amount"))
    exponent = a.number(price.get("exponent"))
    if (
        amount is not None
        and amount >= 0
        and exponent is not None
        and int(exponent) == exponent
        and 0 <= exponent <= 6
        and price.get("currency") == "USD"
    ):
        identity.update(
            monthly_usd=amount,
            price_currency="USD",
            billing_interval="month",
            price_provenance="codex_pricing_configuration_list_price",
        )
    rows = [identity]
    window = result.get("rate_limit") or {}
    for name in ("primary", "secondary"):
        w = window.get(name + "_window")
        if not isinstance(w, dict):
            continue
        rows.append(
            dict(
                base,
                t="quota",
                uid=a.digest("codex-billing-limit", installation, ts, name),
                limit_id="codex",
                window=name,
                used_percent=a.number(w.get("used_percent")),
                window_minutes=(
                    w.get("limit_window_seconds") / 60
                    if a.number(w.get("limit_window_seconds")) is not None
                    else None
                ),
                resets_at=a.number(w.get("reset_at")),
                event="snapshot",
                provenance="codex_billing_api",
            )
        )
    count = a.number(result.get("available_count"))
    if count is not None:
        rows.append(
            dict(
                base,
                t="quota",
                uid=a.digest("codex-billing-inventory", fields["account_key"], ts),
                limit_id="codex",
                window="reset_credits",
                reset_credits_available=count,
                event="reset_credit_snapshot",
                provenance="codex_billing_api",
            )
        )
    for event in result.get("resets", []):
        occurred = timestamp(event.get("occurred_at"))
        kind = event.get("kind")
        ident = event.get("id")
        if (
            occurred is None
            or occurred > ts
            or kind not in ("used", "granted", "expired", "revoked", "purchased")
            or not isinstance(ident, str)
            or not ident
            or len(ident) > 512
        ):
            continue
        rows.append(
            dict(
                base,
                t="quota",
                uid=a.digest("codex-reset", fields["account_key"], ident),
                limit_id="codex",
                window="reset_history",
                occurred_at=occurred,
                event="reset_credit_" + kind,
                provenance="codex_reset_history_api",
            )
        )
    return rows
