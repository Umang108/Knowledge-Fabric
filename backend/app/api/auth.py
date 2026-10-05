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
