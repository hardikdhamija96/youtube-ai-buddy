"""Minimal allowlist auth: users from env, signed session cookie, login rate limit.

AUTH_USERS="alice:password1,bob:password2"   (only these people can log in)
SECRET_KEY=<long random string>               (signs the session cookie)
COOKIE_SECURE=1                               (set when served over HTTPS)
Fails closed: if AUTH_USERS or SECRET_KEY is missing, everything but /health is refused.
"""
import base64
import hashlib
import hmac
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

log = logging.getLogger("uvicorn.error")

COOKIE = "session"
SESSION_SECONDS = 7 * 24 * 3600
PUBLIC_PATHS = {"/health", "/login", "/login.html", "/auth/login", "/favicon.ico"}
MAX_FAILS, WINDOW = 5, 15 * 60

_fails: dict[str, list[float]] = {}


def _users() -> dict[str, str]:
    out = {}
    for part in os.getenv("AUTH_USERS", "").split(","):
        if ":" in part:
            name, pw = part.split(":", 1)
            if name.strip() and pw:
                out[name.strip().lower()] = pw
    return out


def _secret() -> bytes:
    return os.getenv("SECRET_KEY", "").encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(user: str) -> str:
    payload = f"{user}|{int(time.time()) + SESSION_SECONDS}"
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + _sign(payload)


def read_token(token: str | None) -> str | None:
    """Return the username if the token is valid, unexpired and the user is still allowed."""
    if not token or "." not in token or not _secret():
        return None
    b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(b64.encode()).decode()
        user, exp = payload.rsplit("|", 1)
        if not hmac.compare_digest(sig, _sign(payload)) or int(exp) < time.time():
            return None
    except Exception:
        return None
    return user if user in _users() else None  # removing a user from .env revokes access


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _blocked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < WINDOW]
    return len(_fails[ip]) >= MAX_FAILS


class LoginRequest(BaseModel):
    username: str
    password: str


def install(app: FastAPI) -> None:
    if not _users() or len(_secret()) < 16:
        log.error("AUTH_USERS / SECRET_KEY not configured: all routes except /health are refused.")

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)
        if not _users() or len(_secret()) < 16:
            return JSONResponse({"detail": {"code": "auth_not_configured", "message": "Auth is not configured."}}, status_code=503)
        user = read_token(request.cookies.get(COOKIE))
        if user:
            request.state.user = user
            return await call_next(request)
        wants_html = request.method == "GET" and "text/html" in request.headers.get("accept", "")
        if wants_html:
            return RedirectResponse("/login")
        return JSONResponse({"detail": {"code": "unauthorized", "message": "Please log in."}}, status_code=401)

    @app.get("/login")
    def login_page():
        from pathlib import Path
        from fastapi.responses import FileResponse
        return FileResponse(Path(os.getenv("FRONTEND_DIR", "/app/frontend")) / "login.html")

    @app.post("/auth/login")
    def login(req: LoginRequest, request: Request):
        ip = _client_ip(request)
        if _blocked(ip):
            return JSONResponse({"detail": {"code": "rate_limited", "message": "Too many attempts. Try again in 15 minutes."}}, status_code=429)
        name = req.username.strip().lower()
        expected = _users().get(name)
        # compare against a dummy when the user is unknown so timing doesn't reveal valid names
        ok = hmac.compare_digest((expected or "\0dummy").encode(), req.password.encode()) and expected is not None
        if not ok:
            _fails.setdefault(ip, []).append(time.time())
            return JSONResponse({"detail": {"code": "bad_login", "message": "Wrong username or password."}}, status_code=401)
        _fails.pop(ip, None)
        resp = JSONResponse({"user": name})
        resp.set_cookie(COOKIE, make_token(name), max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                        secure=os.getenv("COOKIE_SECURE") == "1", path="/")
        return resp

    @app.post("/auth/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/auth/me")
    def me(request: Request):
        return {"user": request.state.user}
