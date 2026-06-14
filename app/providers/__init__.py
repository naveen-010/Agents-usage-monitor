"""Provider registry. Add a new adapter here and the whole app picks it up."""
from __future__ import annotations

from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider

REGISTRY: dict[str, Provider] = {
    p.name: p for p in (ClaudeProvider(), CodexProvider())
}


def get_provider(name: str) -> Provider | None:
    return REGISTRY.get(name)
