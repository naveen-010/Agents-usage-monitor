"""Codex / ChatGPT usage adapter.

Reads the same rate-limit data Codex CLI shows, from its private endpoint:

    GET https://chatgpt.com/backend-api/codex/usage
        Authorization: Bearer <chatgpt access token>
        ChatGPT-Account-Id: <account id>
        originator: codex_cli_rs

Auth comes from Codex CLI's `~/.codex/auth.json`. Paste the whole file (recommended
— gives us the refresh_token so we can auto-renew) or just the `access_token`.

Auto-renew: when the access token is rejected (401/403) and we have a refresh_token,
we POST to OpenAI's OAuth token endpoint (grant_type=refresh_token, the public Codex
CLI client id) to mint a fresh access token, persist it, and retry — so the token
never needs re-pasting as long as the refresh_token stays valid.

chatgpt.com / auth.openai.com are behind Cloudflare, so we impersonate Chrome.

NOTE: undocumented; access/refresh tokens are full account credentials — secrets.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession, Response

from .base import Metric, Provider, ProviderResult

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # public Codex CLI OAuth client id
IMPERSONATE = "chrome124"  # pinned — see note in claude.py (Cloudflare flags newest)

_HEADERS = {
    "Accept": "application/json",
    "originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.50.0",
}


def _clean(raw: str | None) -> str:
    k = (raw or "").strip().strip('"').strip("'").strip()
    if k.lower().startswith("bearer "):
        k = k[7:].strip()
    return k


def _account_id_from_jwt(token: str) -> str | None:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    auth = claims.get("https://api.openai.com/auth", {})
    if isinstance(auth, dict):
        return auth.get("chatgpt_account_id") or auth.get("organization_id")
    return None


def _normalize_creds(creds: dict[str, Any]) -> None:
    """If the user pasted a whole auth.json, pull out the token fields."""
    at = creds.get("access_token")
    if isinstance(at, str) and at.strip().startswith("{"):
        try:
            j = json.loads(at)
            tok = j.get("tokens", j)
            if tok.get("access_token"):
                creds["access_token"] = tok["access_token"]
            if tok.get("refresh_token"):
                creds["refresh_token"] = tok.get("refresh_token")
            if tok.get("account_id"):
                creds["account_id"] = tok.get("account_id")
        except Exception:
            pass
    creds["access_token"] = _clean(creds.get("access_token"))


def _is_cloudflare(resp: Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    return "text/html" in ctype and any(
        s in (resp.text or "").lower() for s in ("just a moment", "attention required")
    )


def _coerce_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e12:
        ts /= 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _window_metric(key: str, label: str, window: Any) -> Metric | None:
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if used is None:
        return None
    try:
        used = float(used)
    except (TypeError, ValueError):
        return None
    resets_at = None
    if window.get("resets_in_seconds") is not None:
        try:
            resets_at = (datetime.now(timezone.utc)
                         + timedelta(seconds=float(window["resets_in_seconds"]))).isoformat()
        except (TypeError, ValueError):
            pass
    resets_at = resets_at or _coerce_iso(window.get("resets_at") or window.get("reset_at"))
    return Metric(key=key, label=label, utilization=used, resets_at=resets_at)


def _window_label(window: Any, fallback: str) -> str:
    """Name a limit by its duration; either API slot can hold the weekly limit."""
    if not isinstance(window, dict):
        return fallback
    try:
        seconds = int(window.get("limit_window_seconds"))
    except (TypeError, ValueError):
        return fallback
    if seconds == 5 * 60 * 60:
        return "Session (5h)"
    if seconds == 7 * 24 * 60 * 60:
        return "Weekly"
    if seconds > 0 and seconds % (24 * 60 * 60) == 0:
        return f"Limit ({seconds // (24 * 60 * 60)}d)"
    if seconds > 0 and seconds % (60 * 60) == 0:
        return f"Limit ({seconds // (60 * 60)}h)"
    return fallback


async def _refresh_access_token(refresh_token: str) -> dict[str, Any] | None:
    """Exchange a refresh_token for a fresh token bundle. Returns the JSON or None."""
    body = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid profile email",
    }
    try:
        async with AsyncSession(impersonate=IMPERSONATE, timeout=30.0) as session:
            r = await session.post(TOKEN_URL, json=body,
                                   headers={"Content-Type": "application/json"})
    except CurlError:
        return None
    if r.status_code >= 400:
        return None
    try:
        return r.json()
    except Exception:
        return None


class CodexProvider:
    name = "codex"
    display_name = "Codex / ChatGPT"

    async def _get_usage(self, token: str, account_id: str | None) -> Response:
        headers = dict(_HEADERS)
        headers["Authorization"] = f"Bearer {token}"
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        async with AsyncSession(impersonate=IMPERSONATE, timeout=30.0) as session:
            return await session.get(USAGE_URL, headers=headers)

    async def _usage_with_refresh(self, creds: dict[str, Any]) -> Response:
        """GET usage; on auth failure, refresh the token once and retry.
        Mutates `creds` in place with any renewed tokens (so the caller persists them)."""
        token = creds.get("access_token")
        acct = (creds.get("account_id") or "").strip() or _account_id_from_jwt(token)
        resp = await self._get_usage(token, acct)
        if (resp.status_code in (401, 403) and creds.get("refresh_token")
                and not _is_cloudflare(resp)):
            new = await _refresh_access_token(creds["refresh_token"])
            if new and new.get("access_token"):
                creds["access_token"] = new["access_token"]
                if new.get("refresh_token"):
                    creds["refresh_token"] = new["refresh_token"]
                acct = creds.get("account_id") or _account_id_from_jwt(new["access_token"])
                if acct:
                    creds["account_id"] = acct
                resp = await self._get_usage(new["access_token"], acct)
        return resp

    def _error_message(self, resp: Response) -> str:
        code = resp.status_code
        if _is_cloudflare(resp):
            return f"Blocked by Cloudflare (HTTP {code}) — bot protection, not your token."
        snippet = (resp.text or "")[:160].replace("\n", " ").strip()
        if code in (401, 403):
            return (f"Token rejected (HTTP {code}). Paste the whole ~/.codex/auth.json "
                    f"so auto-refresh works." + (f" Server said: {snippet}" if snippet else ""))
        return f"HTTP {code} from chatgpt.com" + (f": {snippet}" if snippet else "")

    async def validate(self, creds: dict[str, Any]) -> tuple[bool, str]:
        _normalize_creds(creds)
        if not creds.get("access_token") and not creds.get("refresh_token"):
            return False, "Paste your ~/.codex/auth.json (or its access_token)."
        # If only a refresh token survived, mint an access token first.
        if not creds.get("access_token") and creds.get("refresh_token"):
            new = await _refresh_access_token(creds["refresh_token"])
            if new and new.get("access_token"):
                creds["access_token"] = new["access_token"]
                if new.get("refresh_token"):
                    creds["refresh_token"] = new["refresh_token"]

        try:
            resp = await self._usage_with_refresh(creds)
        except CurlError as e:
            return False, f"Network error: {e}"

        if resp.status_code >= 400:
            return False, self._error_message(resp)
        try:
            data = resp.json()
        except Exception:
            return False, "Bad JSON from chatgpt.com"
        if not isinstance(data, dict) or "rate_limit" not in data:
            return False, "Unexpected response (no rate_limit field)."
        renew = " (auto-refresh on)" if creds.get("refresh_token") else ""
        return True, f"Valid. Plan: {data.get('plan_type', '?')}{renew}"

    async def fetch(self, creds: dict[str, Any]) -> ProviderResult:
        _normalize_creds(creds)
        if not creds.get("access_token") and not creds.get("refresh_token"):
            return ProviderResult(self.name, self.display_name, error="No access token configured")

        try:
            resp = await self._usage_with_refresh(creds)
        except CurlError as e:
            return ProviderResult(self.name, self.display_name, error=f"Network error: {e}")

        if resp.status_code >= 400:
            code = resp.status_code
            if code in (401, 403) and not _is_cloudflare(resp):
                msg = ("Token expired and refresh failed — re-paste ~/.codex/auth.json"
                       if creds.get("refresh_token") else "Token expired (re-paste ~/.codex/auth.json)")
            else:
                msg = self._error_message(resp)
            return ProviderResult(self.name, self.display_name, error=msg)

        try:
            data = resp.json()
        except Exception:
            return ProviderResult(self.name, self.display_name, error="Bad JSON from chatgpt.com")

        rl = data.get("rate_limit") if isinstance(data, dict) else None
        if not isinstance(rl, dict):
            return ProviderResult(self.name, self.display_name, error="No rate_limit in response")

        metrics: list[Metric] = []
        for key, label, src in (
            ("primary", "Session (5h)", rl.get("primary_window")),
            ("secondary", "Weekly", rl.get("secondary_window")),
        ):
            m = _window_metric(key, _window_label(src, label), src)
            if m:
                metrics.append(m)
        cr = data.get("code_review_rate_limit")
        if isinstance(cr, dict):
            m = _window_metric("code_review", "Code review", cr.get("primary_window") or cr)
            if m:
                metrics.append(m)

        if not metrics:
            return ProviderResult(self.name, self.display_name,
                                  error="Usage response had no rate-limit windows")

        return ProviderResult(
            provider=self.name,
            display_name=self.display_name,
            metrics=metrics,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )


_provider: Provider = CodexProvider()
