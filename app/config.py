"""Central configuration, loaded once from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _thresholds(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return sorted(set(out))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    secret_key: str = os.getenv("SECRET_KEY", "").strip()
    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "").strip()
    poll_interval_seconds: int = _int("POLL_INTERVAL_SECONDS", 120)  # 2 min
    usage_thresholds: list[int] = field(
        default_factory=lambda: _thresholds(os.getenv("USAGE_THRESHOLDS", "75,80,90,100"))
    )

    # ntfy
    ntfy_url: str = os.getenv("NTFY_URL", "https://ntfy.sh").strip().rstrip("/")
    ntfy_topic: str = os.getenv("NTFY_TOPIC", "").strip()
    ntfy_token: str = os.getenv("NTFY_TOKEN", "").strip()

    # telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # discord (channel webhook URL)
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    # Public HTTPS base URL (e.g. https://host.ts.net) so the phone can reach
    # the refresh endpoint from a notification action button.
    public_url: str = os.getenv("PUBLIC_URL", "").strip().rstrip("/")

    @property
    def refresh_token(self) -> str:
        """Secret token gating the unauthenticated /api/refresh action."""
        env = os.getenv("REFRESH_TOKEN", "").strip()
        if env:
            return env
        p = self.data_dir / "refresh.token"
        if p.exists():
            return p.read_text().strip()
        import secrets
        tok = secrets.token_urlsafe(24)
        p.write_text(tok)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return tok

    @property
    def db_path(self) -> Path:
        return self.data_dir / "usage.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def auth_required(self) -> bool:
        return bool(self.dashboard_password)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
