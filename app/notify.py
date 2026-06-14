"""Outbound notifications: ntfy (push) and Telegram. Both are best-effort."""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("notify")


async def _send_ntfy(title: str, message: str, priority: str = "default",
                     tags: str = "", actions: list[dict] | None = None,
                     click: str | None = None) -> None:
    if not settings.ntfy_topic:
        return
    headers = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    # When we have action buttons, ntfy's JSON publish format is cleanest.
    payload: dict = {"topic": settings.ntfy_topic, "title": title, "message": message}
    if tags:
        payload["tags"] = tags.split(",")
    prio_map = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}
    payload["priority"] = prio_map.get(priority, 3)
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(settings.ntfy_url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.warning("ntfy send failed: %s", e)


async def _send_telegram(title: str, message: str) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    text = f"*{title}*\n{message}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(url, json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            })
    except httpx.HTTPError as e:
        log.warning("telegram send failed: %s", e)


async def _send_discord(title: str, message: str) -> None:
    if not settings.discord_webhook_url:
        return
    content = f"**{title}**\n{message}"[:1900]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(settings.discord_webhook_url, json={"content": content})
    except httpx.HTTPError as e:
        log.warning("discord send failed: %s", e)


async def notify(title: str, message: str, priority: str = "default", tags: str = "",
                 actions: list[dict] | None = None, click: str | None = None) -> None:
    """Fan out a single alert to all configured channels."""
    await _send_ntfy(title, message, priority, tags, actions, click)
    await _send_telegram(title, message)
    await _send_discord(title, message)


def channels_enabled() -> list[str]:
    out = []
    if settings.ntfy_topic:
        out.append("ntfy")
    if settings.telegram_bot_token and settings.telegram_chat_id:
        out.append("telegram")
    if settings.discord_webhook_url:
        out.append("discord")
    return out
