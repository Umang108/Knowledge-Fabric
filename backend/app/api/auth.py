from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app import auth
from app.auth import CurrentUser, current_user
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=72)  # bcrypt limit


def _user_view(user: CurrentUser) -> dict:
    return {"user_id": user.user_id, "display_name": user.display_name, "email": user.email}


@router.get("/config")
def auth_config():
    """Tells the frontend which sign-in screen to show."""
    provider = get_settings().auth_provider
    return {"provider": provider, "login_url": "/api/auth/login" if provider == "keycloak" else None}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    """Local sign-in: checks the password and starts a server-side session (cookie)."""
    if get_settings().auth_provider != "local":
        raise HTTPException(400, "Password sign-in is disabled; sign in through Keycloak")
    user = auth.local_login(body.user_id.strip(), body.password)
    auth.create_session(response, request, user.user_id, "local")
    return {"user": _user_view(user)}


@router.get("/login")
def keycloak_login(request: Request, next: str | None = None):
    """Keycloak sign-in: redirects the browser to Keycloak's login page."""
    if get_settings().auth_provider != "keycloak":
        raise HTTPException(400, "Keycloak sign-in is not enabled")
    return RedirectResponse(auth.keycloak_authorize_url(request, next), status_code=302)


@router.get("/callback")
def keycloak_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """Keycloak redirects here after sign-in; the backend finishes the flow and sets the session cookie."""
    if error or not code or not state:
        message = error_description or error or "Sign-in was not completed"
        return RedirectResponse(f"/login?error={quote(message)}", status_code=302)
    try:
        user, tokens, next_path = auth.keycloak_callback(code, state, request)
    except HTTPException as exc:
        return RedirectResponse(f"/login?error={quote(str(exc.detail))}", status_code=302)
    response = RedirectResponse(next_path, status_code=302)
    auth.create_session(response, request, user.user_id, "keycloak", tokens)
    return response


@router.post("/logout")
def logout(request: Request, response: Response):
    """Ends the session on the server (and the Keycloak session, for Keycloak sign-ins)."""
    token = request.cookies.get(get_settings().session_cookie_name)
    if token:
        row = auth.revoke_session(auth.hash_token(token), "logout")
        if row and row["auth_source"] == "keycloak":
            auth.keycloak_logout(row["kc_refresh_token"])
    auth.clear_cookie(response)
    return {"signed_out": True}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    return _user_view(user)


# ------------------------------------------------------------------ session management
@router.get("/session")
def session_status(request: Request):
    """Expiry times of the current session; does not count as activity (the UI polls it)."""
    s = get_settings()
    timing = auth.session_timing(request.cookies.get(s.session_cookie_name))
    if not timing:
        raise HTTPException(401, "Not signed in or the session has expired")
    return {**timing, "idle_minutes": s.session_idle_minutes, "max_hours": s.session_max_hours}


@router.get("/sessions")
def list_sessions(user: CurrentUser = Depends(current_user)):
    """The user's signed-in sessions (browsers / devices)."""
    return [
        {
            "id": auth.public_session_id(row["id"]),
            "current": row["id"] == user.session_id,
            "device": auth.describe_agent(row["user_agent"]),
            "ip_address": row["ip_address"],
            "auth_source": row["auth_source"],
            "signed_in_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
            "expires_at": min(row["expires_at"], row["idle_expires_at"]),
        }
        for row in auth.active_sessions(user.user_id)
    ]


@router.post("/sessions/{public_id}/revoke")
def revoke_one(public_id: str, response: Response, user: CurrentUser = Depends(current_user)):
    for row in auth.active_sessions(user.user_id):
        if auth.public_session_id(row["id"]) == public_id:
            ended = auth.revoke_session(row["id"], "signed out from the sessions page", user.user_id)
            if ended and ended["auth_source"] == "keycloak":
                auth.keycloak_logout(ended["kc_refresh_token"])
            if row["id"] == user.session_id:
                auth.clear_cookie(response)
            return {"revoked": public_id, "current": row["id"] == user.session_id}
    raise HTTPException(404, "Session not found")


@router.post("/sessions/revoke-others")
def revoke_others(user: CurrentUser = Depends(current_user)):
    n = 0
    for row in auth.active_sessions(user.user_id):
        if row["id"] != user.session_id:
            ended = auth.revoke_session(row["id"], "signed out from another session", user.user_id)
            if ended and ended["auth_source"] == "keycloak":
                auth.keycloak_logout(ended["kc_refresh_token"])
            n += 1
    return {"revoked": n}
