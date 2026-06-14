"""FastAPI app: serves the PWA and a small JSON API."""
from __future__ import annotations

import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .config import settings
from .notify import channels_enabled, notify
from .providers import REGISTRY, get_provider
from .scheduler import poll_once, send_snapshot, shutdown_scheduler, start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# In-memory set of valid session tokens (single-process app, fine for one user).
_sessions: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if not settings.auth_required:
        log.warning("DASHBOARD_PASSWORD is empty — dashboard is UNPROTECTED.")
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Usage Dashboard", lifespan=lifespan)


# ---- Auth ----

def require_auth(session: str | None = Cookie(default=None)) -> None:
    if not settings.auth_required:
        return
    if session and session in _sessions:
        return
    raise HTTPException(status_code=401, detail="Authentication required")


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: LoginBody, response: Response):
    if not settings.auth_required:
        return {"ok": True, "auth": False}
    if not hmac.compare_digest(body.password, settings.dashboard_password):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return {"ok": True, "auth": True}


@app.get("/api/me")
async def me(session: str | None = Cookie(default=None)):
    authed = (not settings.auth_required) or (session in _sessions if session else False)
    return {"auth_required": settings.auth_required, "authenticated": authed}


# ---- Usage data ----

@app.get("/api/usage")
async def usage(_: None = Depends(require_auth)):
    """Latest sample per provider/metric, grouped by provider."""
    samples = db.latest_samples()
    configured = set(db.list_credential_providers())
    providers: dict[str, dict] = {}
    for name, prov in REGISTRY.items():
        providers[name] = {
            "provider": name,
            "display_name": prov.display_name,
            "configured": name in configured,
            "error": db.get_state(f"{name}:last_error") if name in configured else None,
            "metrics": [],
        }
    for s in samples:
        providers.setdefault(s["provider"], {
            "provider": s["provider"], "display_name": s["provider"],
            "configured": True, "error": None, "metrics": [],
        })["metrics"].append({
            "key": s["metric_key"],
            "label": s["label"],
            "utilization": s["utilization"],
            "resets_at": s["resets_at"],
            "fetched_at": s["fetched_at"],
        })
    return {"providers": list(providers.values())}


@app.get("/api/history")
async def get_history(provider: str, metric: str, hours: int = 24, _: None = Depends(require_auth)):
    return {"points": db.history(provider, metric, hours)}


@app.post("/api/poll")
async def trigger_poll(_: None = Depends(require_auth)):
    return {"results": await poll_once()}


@app.post("/api/refresh")
async def refresh(token: str = ""):
    """Token-gated refresh for notification action buttons (no session needed)."""
    if not hmac.compare_digest(token, settings.refresh_token):
        raise HTTPException(status_code=403, detail="Invalid refresh token")
    await poll_once()
    await send_snapshot()
    return {"ok": True}


@app.post("/api/notify/snapshot")
async def notify_snapshot(_: None = Depends(require_auth)):
    return {"sent_to": await send_snapshot()}


@app.get("/api/notify/test")
async def notify_test(_: None = Depends(require_auth)):
    await notify("Usage Dashboard", "Test notification — channels are working.", tags="white_check_mark")
    return {"sent_to": channels_enabled()}


# ---- Credentials ----

class CredsBody(BaseModel):
    provider: str
    session_key: str | None = None
    org_id: str | None = None
    access_token: str | None = None
    account_id: str | None = None


_CRED_FIELDS = ("session_key", "org_id", "access_token", "account_id")


@app.post("/api/credentials")
async def set_credentials(body: CredsBody, _: None = Depends(require_auth)):
    provider = get_provider(body.provider)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    creds = {k: getattr(body, k) for k in _CRED_FIELDS if getattr(body, k)}
    valid, message = await provider.validate(creds)
    if not valid:
        raise HTTPException(status_code=400, detail=message)
    db.save_credentials(body.provider, creds)  # creds may now include resolved org_id
    return {"ok": True, "message": message}


@app.delete("/api/credentials/{provider}")
async def remove_credentials(provider: str, _: None = Depends(require_auth)):
    db.delete_credentials(provider)
    return {"ok": True}


@app.get("/api/config")
async def get_config(_: None = Depends(require_auth)):
    return {
        "poll_interval_seconds": settings.poll_interval_seconds,
        "thresholds": settings.usage_thresholds,
        "notify_channels": channels_enabled(),
        "providers": [{"name": p.name, "display_name": p.display_name} for p in REGISTRY.values()],
    }


# ---- Static / PWA ----

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
async def service_worker():
    # Served from root so its scope covers the whole app.
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
