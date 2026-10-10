"""Authentication and server-side sessions.

The browser never holds a token. After sign-in the backend creates a row in `sessions` and sets an
HttpOnly cookie containing a random session token (only its SHA-256 hash is stored). Every request is
authenticated by `current_user`, which looks the session up, enforces idle and absolute expiry, and
checks that the user is still active.

AUTH_PROVIDER=local     user ID + password checked against `users` (bcrypt).
AUTH_PROVIDER=keycloak  the backend runs the OpenID Connect authorization-code flow with PKCE:
                        /api/auth/login redirects to Keycloak, /api/auth/callback exchanges the code,
                        verifies the ID token and creates the session. Keycloak's refresh token is kept
                        encrypted in the session row and used to re-check the user with Keycloak when the
                        access token expires, so a user disabled or signed out in Keycloak loses access.
"""

import base64
import contextlib
import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlencode

import bcrypt
import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response, status

from app.config import Settings, get_settings
from app.db import get_conn

# Checked against when the user doesn't exist, so response time doesn't reveal valid user IDs.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()
@dataclass
class CurrentUser:
    user_id: str
    display_name: str
    email: str | None
    session_id: str | None = None


SESSION_ENDED = "Your session has ended"


def _unauthorized(detail: str = "Not signed in or the session has expired") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        ok = bcrypt.checkpw(password.encode(), (password_hash or _DUMMY_HASH).encode())
    except ValueError:
        return False
    return ok and password_hash is not None


# ------------------------------------------------------------------ encryption of stored Keycloak tokens
def _fernet() -> Fernet:
    key = hashlib.sha256(get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:  # SECRET_KEY changed since the session was created
        return None


# ------------------------------------------------------------------ sessions
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    response: Response, request: Request, user_id: str, auth_source: str, kc_tokens: dict | None = None
) -> str:
    """Store a new session and set the session cookie on `response`. Returns the session id (hash)."""
    s = get_settings()
    token = secrets.token_urlsafe(32)
    session_id = hash_token(token)
    now = _now()
    kc = kc_tokens or {}
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sessions (id, user_id, auth_source, expires_at, idle_expires_at, ip_address, user_agent,
                                     kc_refresh_token, kc_id_token, kc_access_expires_at, modified_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                session_id,
                user_id,
                auth_source,
                now + dt.timedelta(hours=s.session_max_hours),
                now + dt.timedelta(minutes=s.session_idle_minutes),
                request.client.host if request.client else None,
                (request.headers.get("user-agent") or "")[:300],
                encrypt(kc.get("refresh_token")),
                encrypt(kc.get("id_token")),
                now + dt.timedelta(seconds=int(kc["expires_in"])) if kc.get("expires_in") else None,
                user_id,
            ),
        )
        conn.execute("UPDATE users SET last_login_at = now() WHERE user_id = %s", (user_id,))
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=s.session_max_hours * 3600,
        httponly=True,
        secure=s.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return session_id


def revoke_session(session_id: str, reason: str, actor: str | None = None) -> dict | None:
    """actor None means the session's own user (sign-out)."""
    with get_conn() as conn:
        return conn.execute(
            """UPDATE sessions SET revoked_at = now(), revoked_reason = %s, modified_by = coalesce(%s, user_id)
               WHERE id = %s AND revoked_at IS NULL
               RETURNING auth_source, kc_refresh_token""",
            (reason, actor, session_id),
        ).fetchone()


def revoke_user_sessions(user_id: str, reason: str, actor: str) -> int:
    with get_conn() as conn:
        return conn.execute(
            """UPDATE sessions SET revoked_at = now(), revoked_reason = %s, modified_by = %s
               WHERE user_id = %s AND revoked_at IS NULL""",
            (reason, actor, user_id),
        ).rowcount


def public_session_id(session_id: str) -> str:
    """What the UI and traces see for a session: derived from, never equal to, the sessions table key."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def describe_agent(user_agent: str | None) -> str:
    ua = user_agent or ""
    browser = next(
        (name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"), ("Firefox/", "Firefox"),
                                ("Safari/", "Safari")) if key in ua),
        "Unknown browser" if ua else "Unknown client",
    )
    system = next(
        (name for key, name in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iOS"),
                                ("Mac OS X", "macOS"), ("Linux", "Linux")) if key in ua),
        "",
    )
    return f"{browser} on {system}" if system else browser


def active_sessions(user_id: str) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM sessions WHERE user_id = %s AND revoked_at IS NULL
                      AND expires_at > now() AND idle_expires_at > now()
               ORDER BY last_seen_at DESC""",
            (user_id,),
        ).fetchall()


def session_timing(token: str | None) -> dict | None:
    """Expiry of the session behind a cookie, without extending it (for the idle-timeout warning)."""
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT expires_at, idle_expires_at, revoked_at FROM sessions WHERE id = %s", (hash_token(token),)
        ).fetchone()
    now = _now()
    if not row or row["revoked_at"] or now >= row["expires_at"] or now >= row["idle_expires_at"]:
        return None
    return {"server_time": now, "idle_expires_at": row["idle_expires_at"], "expires_at": row["expires_at"]}


def clear_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(
        s.session_cookie_name, path="/", httponly=True, secure=s.session_cookie_secure, samesite="lax"
    )


def current_user(request: Request) -> CurrentUser:
    """FastAPI dependency used by every protected route."""
    s = get_settings()
    token = request.cookies.get(s.session_cookie_name)
    if not token:
        raise _unauthorized()
    session_id = hash_token(token)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT se.*, u.display_name, u.email, u.is_active
               FROM sessions se JOIN users u ON u.user_id = se.user_id WHERE se.id = %s""",
            (session_id,),
        ).fetchone()
    now = _now()
    if not row or row["revoked_at"] or now >= row["expires_at"] or now >= row["idle_expires_at"]:
        raise _unauthorized(SESSION_ENDED)  # a cookie was sent: the session timed out or was signed out
    if not row["is_active"]:
        revoke_session(session_id, "user deactivated", "system")
        raise _unauthorized("User is disabled")
    if row["auth_source"] != s.auth_provider:  # sign-in mode was switched; old sessions don't carry over
        revoke_session(session_id, "auth provider changed", "system")
        raise _unauthorized()
    if row["auth_source"] == "keycloak" and row["kc_access_expires_at"] and now >= row["kc_access_expires_at"]:
        _refresh_keycloak_session(session_id, row, s)
    idle = dt.timedelta(minutes=s.session_idle_minutes)
    resolution = dt.timedelta(seconds=get_settings().auth_last_seen_resolution_seconds)
    if now - row["last_seen_at"] > resolution or row["idle_expires_at"] - now < idle - resolution:
        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = now(), idle_expires_at = %s WHERE id = %s",
                (now + dt.timedelta(minutes=s.session_idle_minutes), session_id),
            )
    return CurrentUser(row["user_id"], row["display_name"], row["email"], session_id)


# ------------------------------------------------------------------ local sign-in
def local_login(user_id: str, password: str) -> CurrentUser:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, display_name, email, password_hash, is_active FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if not verify_password(password, row["password_hash"] if row else None) or not row["is_active"]:
        raise _unauthorized("Incorrect user ID or password")
    return CurrentUser(row["user_id"], row["display_name"], row["email"])


# ------------------------------------------------------------------ Keycloak (OpenID Connect)
def keycloak_issuer(s: Settings) -> str:
    return f"{s.keycloak_url.rstrip('/')}/realms/{s.keycloak_realm}"


def _oidc_backchannel(s: Settings) -> str:
    base = (s.keycloak_internal_url or s.keycloak_url).rstrip("/")
    return f"{base}/realms/{s.keycloak_realm}/protocol/openid-connect"


@lru_cache
def _jwks_client(certs_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(certs_url, cache_keys=True, lifespan=get_settings().keycloak_jwks_cache_seconds)


def redirect_uri(request: Request, s: Settings) -> str:
    base = s.app_url.rstrip("/") if s.app_url else str(request.base_url).rstrip("/")
    return f"{base}/api/auth/callback"


def safe_next(path: str | None) -> str:
    """Only same-site relative paths, so the login can't be used as an open redirect."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/workspace"


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def keycloak_authorize_url(request: Request, next_path: str | None) -> str:
    """Start a sign-in: remember state/nonce/verifier server-side and build Keycloak's login URL."""
    s = get_settings()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    with get_conn() as conn:
        conn.execute("DELETE FROM oidc_login_requests WHERE expires_at < now()")
        conn.execute(
            """INSERT INTO oidc_login_requests (state, code_verifier, nonce, next_path, expires_at, modified_by)
               VALUES (%s, %s, %s, %s, %s, 'system')""",
            (
                state,
                verifier,
                nonce,
                safe_next(next_path),
                _now() + dt.timedelta(minutes=get_settings().auth_login_request_ttl_minutes),
            ),
        )
    params = {
        "client_id": s.keycloak_client_id,
        "redirect_uri": redirect_uri(request, s),
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{keycloak_issuer(s)}/protocol/openid-connect/auth?{urlencode(params)}"


def _token_request(s: Settings, data: dict) -> dict:
    data = {"client_id": s.keycloak_client_id, **data}
    if s.keycloak_client_secret:
        data["client_secret"] = s.keycloak_client_secret
    try:
        r = httpx.post(
            f"{_oidc_backchannel(s)}/token",
            data=data,
            timeout=get_settings().keycloak_token_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Keycloak is unreachable") from exc
    if r.status_code != 200:
        raise _unauthorized("Keycloak rejected the sign-in")
    return r.json()


def verify_id_token(id_token: str, s: Settings, nonce: str | None) -> dict:
    try:
        key = _jwks_client(f"{_oidc_backchannel(s)}/certs").get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=s.keycloak_client_id,
            issuer=keycloak_issuer(s),
            options={"require": ["sub", "exp", "iss", "aud"]},
        )
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Keycloak is unreachable") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid Keycloak token") from exc
    if claims.get("typ", "ID") != "ID":  # an access or refresh token is not proof of sign-in
        raise _unauthorized("Invalid Keycloak token")
    if nonce is not None and claims.get("nonce") != nonce:
        raise _unauthorized("Sign-in response does not match the request")
    if not claims.get("preferred_username"):
        raise _unauthorized("Keycloak token has no preferred_username")
    return claims


def keycloak_user(claims: dict) -> CurrentUser:
    """Create the users row on first sign-in; refresh name/email afterwards."""
    user_id = claims["preferred_username"]
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO users (user_id, display_name, email, auth_source, modified_by)
               VALUES (%(u)s, %(n)s, %(e)s, 'keycloak', 'system')
               ON CONFLICT (user_id) DO UPDATE
                 SET display_name = EXCLUDED.display_name, email = EXCLUDED.email,
                     auth_source = 'keycloak', modified_by = 'system'
               RETURNING user_id, display_name, email, is_active""",
            {"u": user_id, "n": claims.get("name") or user_id, "e": claims.get("email")},
        ).fetchone()
    if not row["is_active"]:
        raise _unauthorized("User is disabled")
    return CurrentUser(row["user_id"], row["display_name"], row["email"])


def keycloak_callback(code: str, state: str, request: Request) -> tuple[CurrentUser, dict, str]:
    """Finish a sign-in: returns (user, Keycloak tokens, path to continue to)."""
    s = get_settings()
    with get_conn() as conn:
        pending = conn.execute(
            "DELETE FROM oidc_login_requests WHERE state = %s AND expires_at > now() RETURNING *", (state,)
        ).fetchone()
    if not pending:
        raise _unauthorized("The sign-in request expired or is unknown; please sign in again")
    tokens = _token_request(
        s,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(request, s),
            "code_verifier": pending["code_verifier"],
        },
    )
    claims = verify_id_token(tokens.get("id_token", ""), s, pending["nonce"])
    return keycloak_user(claims), tokens, pending["next_path"]


def _refresh_keycloak_session(session_id: str, row: dict, s: Settings) -> None:
    """Ask Keycloak whether the user is still allowed in; revoke the session if not."""
    refresh = decrypt(row["kc_refresh_token"])
    try:
        tokens = _token_request(s, {"grant_type": "refresh_token", "refresh_token": refresh}) if refresh else None
    except HTTPException as exc:
        if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        tokens = None
    if not tokens:
        revoke_session(session_id, "keycloak session ended", "system")
        raise _unauthorized()
    with get_conn() as conn:
        conn.execute(
            """UPDATE sessions SET kc_refresh_token = %s, kc_id_token = coalesce(%s, kc_id_token),
                      kc_access_expires_at = %s, modified_by = 'system' WHERE id = %s""",
            (
                encrypt(tokens.get("refresh_token") or refresh),
                encrypt(tokens.get("id_token")),
                _now()
                + dt.timedelta(
                    seconds=int(tokens.get("expires_in", get_settings().auth_default_token_expiry_seconds))
                ),
                session_id,
            ),
        )


def keycloak_logout(encrypted_refresh_token: str | None) -> None:
    """End the Keycloak SSO session from the server (back-channel); failures don't block sign-out."""
    s = get_settings()
    refresh = decrypt(encrypted_refresh_token)
    if not refresh:
        return
    data = {"client_id": s.keycloak_client_id, "refresh_token": refresh}
    if s.keycloak_client_secret:
        data["client_secret"] = s.keycloak_client_secret
    with contextlib.suppress(httpx.HTTPError):
        httpx.post(
            f"{_oidc_backchannel(s)}/logout",
            data=data,
            timeout=get_settings().keycloak_logout_timeout_seconds,
        )
