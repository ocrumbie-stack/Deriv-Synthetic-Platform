"""Google sign-in for the dashboard.

Only emails listed in ALLOWED_EMAILS get in, so there is no platform password
to leak or forget - account recovery is Google's. TradingView can't log in,
so the webhook endpoints stay public and rely on WEBHOOK_SECRET instead.

Auth switches on once GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and
ALLOWED_EMAILS are all set, so a deploy never locks the dashboard before the
Google client exists.
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.config import settings

logger = logging.getLogger("uvicorn.error")

SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"
SESSION_MAX_AGE = 30 * 24 * 3600
STATE_MAX_AGE = 10 * 60

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

PUBLIC_PATHS = {
    "/webhook", "/webhook/exit", "/health",
    "/login", "/logout", "/auth/google", "/auth/callback",
    "/static/styles.css",
}

router = APIRouter()


def allowed_emails() -> set[str]:
    return {e.strip().lower() for e in settings.allowed_emails.split(",") if e.strip()}


def auth_enabled() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret and allowed_emails())


def _signing_key() -> bytes:
    # Falls back to a key derived from the Google client secret so there's one
    # less variable to set; setting SESSION_SECRET (or changing it) signs out
    # every device.
    secret = settings.session_secret or f"session:{settings.google_client_secret}"
    return hashlib.sha256(secret.encode()).digest()


def _sign(data: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    return data


def current_email(request: Request) -> str | None:
    data = _unsign(request.cookies.get(SESSION_COOKIE))
    email = (data or {}).get("email")
    # Re-checked on every request so removing an email from ALLOWED_EMAILS
    # revokes that account's existing sessions too.
    return email if email in allowed_emails() else None


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _redirect_uri(request: Request) -> str:
    if settings.public_url:
        return settings.public_url.rstrip("/") + "/auth/callback"
    scheme = "https" if _is_https(request) else "http"
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}/auth/callback"


async def require_login(request: Request, call_next) -> Response:
    if not auth_enabled() or request.url.path in PUBLIC_PATHS or current_email(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not signed in."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


LOGIN_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Sign in — Deriv Synthetic</title>
    <link rel="stylesheet" href="/static/styles.css" />
    <style>
      .login { display: grid; place-items: center; min-height: 100vh; padding: 16px; }
      .login-card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
                    display: grid; gap: 16px; max-width: 360px; padding: 28px; text-align: center; width: 100%; }
      .login-card h1 { font-size: 18px; }
      .login-card p { color: var(--muted); }
      .login-error { background: var(--red-dim); border-radius: 6px; color: var(--red); padding: 10px; }
      .login-btn { background: var(--yellow); border-radius: 6px; color: #111; display: block;
                   font-weight: 600; padding: 11px; text-decoration: none; }
    </style>
  </head>
  <body>
    <main class="login">
      <div class="login-card">
        <h1>Deriv Synthetic</h1>
        <p>Sign in to open the trading dashboard.</p>
        {error}
        <a class="login-btn" href="/auth/google">Sign in with Google</a>
      </div>
    </main>
  </body>
</html>"""


@router.get("/login")
def login(request: Request, error: str | None = None) -> Response:
    if not auth_enabled() or current_email(request):
        return RedirectResponse("/", status_code=303)
    block = f'<div class="login-error">{html.escape(error)}</div>' if error else ""
    return HTMLResponse(LOGIN_PAGE.replace("{error}", block), headers={"Cache-Control": "no-store"})


def _login_error(message: str) -> RedirectResponse:
    return RedirectResponse("/login?" + urlencode({"error": message}), status_code=303)


@router.get("/auth/google")
def auth_google(request: Request) -> Response:
    if not auth_enabled():
        return RedirectResponse("/", status_code=303)
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=303)
    response.set_cookie(
        STATE_COOKIE, state, max_age=STATE_MAX_AGE, httponly=True, samesite="lax", secure=_is_https(request)
    )
    return response


@router.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None) -> Response:
    if not auth_enabled():
        return RedirectResponse("/", status_code=303)
    if error or not code:
        return _login_error("Google sign-in was cancelled.")
    expected_state = request.cookies.get(STATE_COOKIE)
    if not state or not expected_state or not hmac.compare_digest(state, expected_state):
        return _login_error("Sign-in expired - please try again.")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await client.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": _redirect_uri(request),
                "grant_type": "authorization_code",
            })
            token.raise_for_status()
            info = await client.get(
                GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {token.json()['access_token']}"}
            )
            info.raise_for_status()
            user = info.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Google sign-in failed: %s", exc)
        return _login_error("Could not reach Google - please try again.")

    email = str(user.get("email") or "").lower()
    if not user.get("email_verified") or email not in allowed_emails():
        logger.warning("Refused dashboard sign-in for %s", email or "unknown account")
        return _login_error(f"{email or 'That account'} is not allowed to access this dashboard.")

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _sign({"email": email, "exp": int(time.time()) + SESSION_MAX_AGE}),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=_is_https(request),
    )
    response.delete_cookie(STATE_COOKIE)
    return response


@router.get("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/api/me")
def me(request: Request) -> dict:
    return {"email": current_email(request), "auth_enabled": auth_enabled()}
