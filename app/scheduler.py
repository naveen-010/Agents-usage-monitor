"""Background polling loop: fetch usage, store samples, fire notifications.

Notification rules (per provider+metric, deduped in `notify_state`):
  * Reset detected      — utilization drops sharply (new window began).
  * Reset soon          — a window's resets_at is < 60 min away (once per window).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db
from .config import settings
from .notify import notify
from .providers import REGISTRY

log = logging.getLogger("scheduler")

RESET_DROP = 20.0          # absolute % drop that counts as a window reset
RESET_SOON_MINUTES = 60
_NEW_WINDOW_TOLERANCE_S = 10 * 60  # resets_at must move > this to count as a new window

_scheduler: AsyncIOScheduler | None = None


def _epoch(resets_at: str | None) -> float | None:
    if not resets_at:
        return None
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _minutes_until(resets_at: str | None) -> float | None:
    e = _epoch(resets_at)
    if e is None:
        return None
    return (e - datetime.now(timezone.utc).timestamp()) / 60.0


def _remaining_summary() -> str:
    """Multi-line 'X% left' for every tracked window (the full picture)."""
    rows = db.latest_samples()
    lines = []
    for r in rows:
        left = max(0.0, 100.0 - float(r["utilization"]))
        lines.append(f"{r['provider']} · {r['label']}: {left:.0f}% left")
    return "\n".join(lines)


def _with_summary(reason: str) -> str:
    """Append the full remaining-budget snapshot under the reason line."""
    s = _remaining_summary()
    return f"{reason}\n\n{s}" if s else reason


async def _evaluate_metric(provider: str, display: str, key: str, label: str,
                           util: float, resets_at: str | None) -> None:
    util_key = f"{provider}:{key}:util"
    soon_key = f"{provider}:{key}:soon"  # stores the resets_at epoch we last alerted for

    prev_util_raw = db.get_state(util_key)
    prev_util = float(prev_util_raw) if prev_util_raw is not None else None

    # ---- Reset detected (usage dropped sharply → new window began) ----
    if prev_util is not None and (prev_util - util) >= RESET_DROP:
        await notify(
            f"{display}: {label} reset",
            _with_summary(f"Usage reset to {util:.0f}%. Fresh window started."),
            priority="default", tags="recycle",
        )
        db.set_state(soon_key, None)

    # ---- Reset soon (< 60 min away), once per window ----
    mins = _minutes_until(resets_at)
    if mins is not None and 0 < mins <= RESET_SOON_MINUTES:
        cur_epoch = _epoch(resets_at)
        last_raw = db.get_state(soon_key)
        last_epoch = float(last_raw) if last_raw else None
        # Re-alert only for a genuinely new window — ignore the rolling window's
        # small reset-time drift between polls.
        is_new_window = (last_epoch is None or cur_epoch is None
                         or abs(cur_epoch - last_epoch) > _NEW_WINDOW_TOLERANCE_S)
        if is_new_window:
            await notify(
                f"{display}: {label} resets soon",
                _with_summary(f"Resets in ~{mins:.0f} min (currently {util:.0f}% used)."),
                priority="default", tags="hourglass",
            )
            db.set_state(soon_key, str(cur_epoch) if cur_epoch is not None else (resets_at or ""))

    db.set_state(util_key, str(util))


async def poll_once() -> dict[str, str]:
    """Poll every provider that has stored credentials. Returns {provider: status}."""
    results: dict[str, str] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    for name in db.list_credential_providers():
        provider = REGISTRY.get(name)
        if provider is None:
            continue
        creds = db.get_credentials(name)
        if creds is None:
            results[name] = "no credentials"
            continue

        result = await provider.fetch(creds)
        # Persist any creds the adapter resolved (e.g. org_id).
        db.save_credentials(name, creds)

        if not result.ok:
            db.set_state(f"{name}:last_error", result.error or "unknown error")
            results[name] = f"error: {result.error}"
            log.warning("poll %s failed: %s", name, result.error)
            continue

        db.set_state(f"{name}:last_error", None)  # clear stale error on success

        for m in result.metrics:
            db.insert_sample(name, m.key, m.label, m.utilization, m.resets_at, now_iso)
            try:
                await _evaluate_metric(name, result.display_name, m.key, m.label,
                                       m.utilization, m.resets_at)
            except Exception as e:  # never let notifications break polling
                log.exception("notify eval failed for %s/%s: %s", name, m.key, e)

        results[name] = f"ok ({len(result.metrics)} metrics)"

    db.prune_old_samples(30)
    return results


async def send_snapshot() -> list[str]:
    """Push a current 'budget remaining' snapshot to notification channels,
    with ntfy Refresh + Open action buttons."""
    from .notify import channels_enabled

    message = _remaining_summary() or "No usage data yet."

    dash = settings.public_url
    actions: list[dict] = []
    if dash:
        actions.append({"action": "view", "label": "Open", "url": dash})
        actions.append({
            "action": "http", "label": "↻ Refresh",
            "method": "POST", "url": f"{dash}/api/refresh?token={settings.refresh_token}",
            "clear": True,
        })
    await notify("Usage remaining", message, tags="bar_chart",
                 actions=actions or None, click=dash or None)
    return channels_enabled()


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        poll_once,
        "interval",
        seconds=settings.poll_interval_seconds,
        id="poll",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),  # run immediately on boot
    )
    sched.start()
    _scheduler = sched
    log.info("scheduler started, interval=%ss", settings.poll_interval_seconds)
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
