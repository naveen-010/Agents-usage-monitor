"""Provider contract + normalized data model shared by all adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass
class Metric:
    """One usage window, normalized across providers."""
    key: str                 # stable id, e.g. "five_hour"
    label: str               # human label, e.g. "Session (5h)"
    utilization: float       # 0-100 percent used
    resets_at: str | None    # ISO-8601 string, or None


@dataclass
class ProviderResult:
    provider: str                       # "claude" / "codex"
    display_name: str                   # "Claude"
    metrics: list[Metric] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None            # set when the fetch failed

    @property
    def ok(self) -> bool:
        return self.error is None


class Provider(Protocol):
    name: str
    display_name: str

    async def validate(self, creds: dict[str, Any]) -> tuple[bool, str]:
        """Return (is_valid, message). May enrich `creds` in place (e.g. resolve org id)."""
        ...

    async def fetch(self, creds: dict[str, Any]) -> ProviderResult:
        """Fetch current usage. Never raises — returns ProviderResult with `error` set on failure."""
        ...
