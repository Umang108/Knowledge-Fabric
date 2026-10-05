"""Keycloak access tokens for the MCP server.

The MCP server is an OAuth 2.0 resource server: agents send `Authorization: Bearer <access token>` that
Keycloak issued to the person using the agent. Every request's token is checked here:

  * signature against the realm's published keys (JWKS, fetched from KEYCLOAK_INTERNAL_URL and cached)
  * issuer  == {KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}       (the browser-facing URL, as in the web login)
  * audience contains MCP_AUDIENCE                          (Keycloak audience mapper on the agent's client)
  * not expired, typ == "Bearer" (an ID or refresh token is not an access token)
  * every scope in MCP_REQUIRED_SCOPES is present
  * preferred_username is present: it is the Graphbase user_id

Tools then act as that user: `kb.require_access` decides what the user may see, exactly as in the web app.
"""

import logging

import anyio
import jwt
from mcp.server.auth.provider import AccessToken

from app import auth
from app.auth import CurrentUser
from app.config import Settings, get_settings
from app.db import get_conn

log = logging.getLogger(__name__)

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


class KeycloakAccessToken(AccessToken):
    """The SDK's AccessToken plus the verified claims, so tools know who is calling."""

    claims: dict


class TokenRejected(Exception):
    pass


def required_scopes(s: Settings) -> list[str]:
    return [x.strip() for x in s.mcp_required_scopes.split(",") if x.strip()]


def verify_access_token(token: str, s: Settings | None = None) -> dict:
    """Validate a Keycloak access token and return its claims; raises TokenRejected with the reason."""
    s = s or get_settings()
    try:
        key = auth._jwks_client(f"{auth._oidc_backchannel(s)}/certs").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=ALGORITHMS,
            audience=s.mcp_audience,
            issuer=auth.keycloak_issuer(s),
            options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            leeway=30,
        )
    except jwt.PyJWKClientConnectionError as exc:
        raise TokenRejected(f"Keycloak is unreachable: {exc}") from exc
    except jwt.PyJWTError as exc:
        raise TokenRejected(f"{type(exc).__name__}: {exc}") from exc
    if claims.get("typ", "Bearer").lower() != "bearer":
        raise TokenRejected(f"not an access token (typ={claims.get('typ')})")
    if not claims.get("preferred_username"):
        raise TokenRejected("token has no preferred_username")
    granted = set(str(claims.get("scope", "")).split())
    missing = [x for x in required_scopes(s) if x not in granted]
    if missing:
        raise TokenRejected(f"token lacks scope(s): {', '.join(missing)}")
    return claims


class KeycloakTokenVerifier:
    """mcp TokenVerifier: returns None (HTTP 401 for the client) for any token Keycloak didn't issue to us."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(verify_access_token, token)
        except TokenRejected as exc:
            log.info("MCP token rejected: %s", exc)
            return None
        return KeycloakAccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            claims=claims,
        )


def user_for_claims(claims: dict) -> CurrentUser:
    """The Graphbase user behind a verified token. A Keycloak user who has never signed in to the web app
    gets a users row (MCP_AUTO_PROVISION_USERS), with no access to anything until an owner grants it."""
    user_id = claims["preferred_username"]
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, display_name, email, is_active FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        if row is None and get_settings().mcp_auto_provision_users:
            conn.execute(
                """INSERT INTO users (user_id, display_name, email, auth_source, modified_by)
                   VALUES (%s, %s, %s, 'keycloak', 'mcp') ON CONFLICT (user_id) DO NOTHING""",
                (user_id, claims.get("name") or user_id, claims.get("email")),
            )
            row = conn.execute(
                "SELECT user_id, display_name, email, is_active FROM users WHERE user_id = %s", (user_id,)
            ).fetchone()
    if row is None:
        raise PermissionError(f"{user_id} is not a Graphbase user; ask an administrator to add them")
    if not row["is_active"]:
        raise PermissionError(f"{user_id} is disabled in Graphbase")
    return CurrentUser(row["user_id"], row["display_name"], row["email"])
