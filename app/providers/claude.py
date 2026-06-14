"""Claude (claude.ai) usage adapter.

Replays the browser `sessionKey` cookie against claude.ai's private web API —
the same endpoints the website itself uses:

    GET /api/organizations                  -> [{uuid, name}, ...]
    GET /api/organizations/{orgId}/usage     -> {five_hour, seven_day, seven_day_opus, seven_day_sonnet}

Each usage window is {"utilization": <0-100>, "resets_at": "<ISO8601>"}.

claude.ai sits behind Cloudflare, which fingerprints the TLS/HTTP2 handshake and
challenges non-browser clients (plain httpx/requests get a 403 HTML page). We use
`curl_cffi` with Chrome impersonation so the handshake matches a real browser and
passes the WAF.

NOTE: undocumented and can change without notice. The `sessionKey` is a full
account session cookie — treat the stored value as a secret.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession, Response

from .base import Metric, Provider, ProviderResult

BASE_URL = "https://claude.ai"
# Pinned: the "chrome" alias tracks the newest Chrome, whose fingerprint Cloudflare
# flags from datacenter IPs. chrome124 passes reliably (verified 6/6 from the server).
IMPERSONATE = "chrome124"

# Friendly labels for the four windows Claude returns, in display order.
_METRIC_LABELS: list[tuple[str, str]] = [
    ("five_hour", "Session (5h)"),
    ("seven_day", "Weekly (all models)"),
    ("seven_day_opus", "Weekly (Opus)"),
    ("seven_day_sonnet", "Weekly (Sonnet)"),
]

_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
}


def _clean_key(raw: str | None) -> str:
    """Forgive common copy/paste mistakes: whitespace, quotes, name prefix."""
    k = (raw or "").strip().strip('"').strip("'").strip()
    if k.lower().startswith("sessionkey="):
        k = k.split("=", 1)[1].strip()
    return k


def _looks_like_cloudflare_block(resp: Response) -> bool:
    """A WAF challenge returns an HTML page; the real API returns small JSON."""
    ctype = resp.headers.get("content-type", "").lower()
    if "text/html" not in ctype:
        return False
    body = (resp.text or "")[:2000].lower()
    return any(s in body for s in ("just a moment", "attention required", "cf-challenge", "/cdn-cgi/challenge"))


def _http_error_message(resp: Response) -> str:
    code = resp.status_code
    if _looks_like_cloudflare_block(resp):
        return (
            f"Blocked by Cloudflare (HTTP {code}) — claude.ai's bot protection rejected "
            "the request, not your key. This is more likely from a datacenter/VPN IP."
        )
    snippet = (resp.text or "")[:160].replace("\n", " ").strip()
    if code in (401, 403):
        return (
            f"Key rejected (HTTP {code}). Copy the full value of the cookie named exactly "
            f"`sessionKey` (starts with sk-ant-sid01-), not `sessionKeyLC`."
            + (f" Server said: {snippet}" if snippet else "")
        )
    return f"HTTP {code} from claude.ai" + (f": {snippet}" if snippet else "")


class ClaudeProvider:
    name = "claude"
    display_name = "Claude"

    async def _get(self, session_key: str, path: str) -> Response:
        """Single browser-impersonating GET. Raises CurlError on network failure."""
        async with AsyncSession(impersonate=IMPERSONATE, timeout=30.0) as session:
            return await session.get(
                f"{BASE_URL}{path}",
                headers={**_HEADERS, "Cookie": f"sessionKey={session_key}"},
            )

    async def _organizations(self, session_key: str) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Returns (orgs, error_message). Exactly one is non-None."""
        resp = await self._get(session_key, "/api/organizations")
        if resp.status_code >= 400:
            return None, _http_error_message(resp)
        try:
            data = resp.json()
        except Exception:
            return None, "Bad JSON from claude.ai (unexpected response)"
        return (data if isinstance(data, list) else []), None

    async def validate(self, creds: dict[str, Any]) -> tuple[bool, str]:
        session_key = _clean_key(creds.get("session_key"))
        if not session_key:
            return False, "session_key is required"
        creds["session_key"] = session_key  # persist the cleaned value

        try:
            orgs, err = await self._organizations(session_key)
        except CurlError as e:
            return False, f"Network error: {e}"
        if err is not None:
            return False, err
        if not orgs:
            return False, "No organizations returned for this session."

        if not creds.get("org_id"):
            creds["org_id"] = orgs[0].get("uuid", "")
        names = ", ".join(o.get("name", "?") for o in orgs)
        return True, f"Valid. Orgs: {names}"

    async def fetch(self, creds: dict[str, Any]) -> ProviderResult:
        session_key = _clean_key(creds.get("session_key"))
        org_id = (creds.get("org_id") or "").strip()
        if not session_key:
            return ProviderResult(self.name, self.display_name, error="No session key configured")

        try:
            if not org_id:
                orgs, err = await self._organizations(session_key)
                if err is not None:
                    return ProviderResult(self.name, self.display_name, error=err)
                if not orgs:
                    return ProviderResult(self.name, self.display_name, error="No organizations found")
                org_id = orgs[0].get("uuid", "")
                creds["org_id"] = org_id

            resp = await self._get(session_key, f"/api/organizations/{org_id}/usage")
        except CurlError as e:
            return ProviderResult(self.name, self.display_name, error=f"Network error: {e}")

        if resp.status_code >= 400:
            code = resp.status_code
            msg = "Session expired (re-paste key)" if code in (401, 403) and not _looks_like_cloudflare_block(resp) \
                else _http_error_message(resp)
            return ProviderResult(self.name, self.display_name, error=msg)

        try:
            payload = resp.json()
        except Exception:
            return ProviderResult(self.name, self.display_name, error="Bad JSON from claude.ai")

        metrics: list[Metric] = []
        for key, label in _METRIC_LABELS:
            window = payload.get(key)
            if not isinstance(window, dict):
                continue
            util = window.get("utilization")
            if util is None:
                continue
            try:
                util = float(util)
            except (TypeError, ValueError):
                continue
            metrics.append(Metric(
                key=key,
                label=label,
                utilization=util,
                resets_at=window.get("resets_at"),
            ))

        # Pay-as-you-go credit pool, when the account has it enabled.
        eu = payload.get("extra_usage")
        if isinstance(eu, dict) and eu.get("is_enabled") and eu.get("utilization") is not None:
            try:
                metrics.append(Metric("extra_usage", "Extra usage (credits)",
                                      float(eu["utilization"]), None))
            except (TypeError, ValueError):
                pass

        if not metrics:
            return ProviderResult(self.name, self.display_name,
                                  error="Usage response had no recognizable windows")

        return ProviderResult(
            provider=self.name,
            display_name=self.display_name,
            metrics=metrics,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )


_provider: Provider = ClaudeProvider()
