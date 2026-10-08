"""Sign the person using the agent in to Keycloak, and keep their access token fresh.

Two ways to sign in:
  * device code (default, RFC 8628): the agent prints a link and a short code; the person opens it in any
    browser, signs in with their normal Keycloak login (SSO, MFA, ...) and the agent receives their tokens.
    Works on servers and terminals without a browser.
  * password (Resource Owner Password grant): for scripts and tests. Needs "Direct access grants" on the
    client in Keycloak; avoid it for real users.

The access token is sent to the TCS Knowledge Fabric MCP server as `Authorization: Bearer ...` by KeycloakAuth, which
refreshes it shortly before it expires and once more if the server answers 401.
"""

import base64
import contextlib
import json
import os
import stat
import threading
import time
from pathlib import Path

import anyio
import httpx

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class LoginError(RuntimeError):
    pass


def _claims(jwt_token: str) -> dict:
    """Read a JWT's payload for display (who is signed in). The MCP server is what verifies tokens."""
    try:
        payload = jwt_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class KeycloakSession:
    def __init__(
        self,
        keycloak_url: str,
        realm: str,
        client_id: str,
        client_secret: str | None = None,
        scope: str = "openid",
        verify: bool | str = True,
        token_cache: Path | None = None,
    ):
        self.issuer = f"{keycloak_url.rstrip('/')}/realms/{realm}"
        self.client_id, self.client_secret, self.scope = client_id, client_secret, scope
        self.verify = verify
        self.token_cache = token_cache
        self._tokens: dict = {}
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self._endpoints: dict | None = None
        if token_cache and token_cache.exists():
            self._load_cache()

    # -------------------------------------------------------------- endpoints
    @property
    def endpoints(self) -> dict:
        if self._endpoints is None:
            r = httpx.get(f"{self.issuer}/.well-known/openid-configuration", verify=self.verify, timeout=15)
            if r.status_code != 200:
                raise LoginError(f"Keycloak realm not found at {self.issuer} (HTTP {r.status_code})")
            self._endpoints = r.json()
        return self._endpoints

    def _client_params(self) -> dict:
        params = {"client_id": self.client_id}
        if self.client_secret:
            params["client_secret"] = self.client_secret
        return params

    def _token_request(self, data: dict) -> dict:
        r = httpx.post(
            self.endpoints["token_endpoint"], data={**self._client_params(), **data}, verify=self.verify, timeout=30
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            raise LoginError(body.get("error_description") or body.get("error") or f"HTTP {r.status_code}")
        return body

    def _store(self, tokens: dict) -> None:
        self._tokens = tokens
        self._expires_at = time.time() + int(tokens.get("expires_in", 60))
        self._save_cache()

    # -------------------------------------------------------------- sign in
    def login_password(self, username: str, password: str) -> dict:
        self._store(
            self._token_request({"grant_type": "password", "username": username, "password": password,
                                 "scope": self.scope})
        )
        return self.user

    def login_device(self, show=print, timeout: float = 600) -> dict:
        """Device code flow. `show(message, verification_uri_complete, user_code)` tells the person what to do."""
        endpoint = self.endpoints.get("device_authorization_endpoint")
        if not endpoint:
            raise LoginError("This Keycloak realm does not offer the device code flow")
        r = httpx.post(endpoint, data={**self._client_params(), "scope": self.scope}, verify=self.verify, timeout=30)
        if r.status_code != 200:
            raise LoginError(f"Keycloak refused the device login: {r.text[:200]}")
        d = r.json()
        link = d.get("verification_uri_complete") or d["verification_uri"]
        show(f"To sign in, open {link} and confirm the code {d['user_code']}", link, d["user_code"])
        interval = max(int(d.get("interval", 5)), 1)
        deadline = time.time() + min(timeout, int(d.get("expires_in", 600)))
        while time.time() < deadline:
            r = httpx.post(
                self.endpoints["token_endpoint"],
                data={**self._client_params(), "grant_type": DEVICE_GRANT, "device_code": d["device_code"]},
                verify=self.verify,
                timeout=30,
            )
            body = r.json()
            if r.status_code == 200:
                self._store(body)
                return self.user
            error = body.get("error")
            if error == "authorization_pending":
                time.sleep(interval)
            elif error == "slow_down":
                interval += 5
                time.sleep(interval)
            else:
                raise LoginError(body.get("error_description") or error or "device login failed")
        raise LoginError("The sign-in link expired before it was used")

    # -------------------------------------------------------------- tokens
    @property
    def user(self) -> dict:
        c = _claims(self._tokens.get("access_token", ""))
        return {"user_id": c.get("preferred_username"), "name": c.get("name"), "email": c.get("email")}

    @property
    def signed_in(self) -> bool:
        return bool(self._tokens.get("access_token"))

    def refresh(self) -> None:
        refresh_token = self._tokens.get("refresh_token")
        if not refresh_token:
            raise LoginError("Not signed in")
        try:
            self._store(self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token}))
        except LoginError as exc:
            self._tokens, self._expires_at = {}, 0.0
            self._save_cache()
            raise LoginError(f"Your Keycloak session ended ({exc}); sign in again") from exc

    def access_token(self, min_validity: float = 30) -> str:
        with self._lock:
            if not self.signed_in:
                raise LoginError("Not signed in")
            if time.time() > self._expires_at - min_validity:
                self.refresh()
            return self._tokens["access_token"]

    def force_refresh(self, used_token: str) -> str:
        """After a 401: refresh once, unless another request already did."""
        with self._lock:
            if self._tokens.get("access_token") == used_token:
                self.refresh()
            return self._tokens["access_token"]

    def logout(self) -> None:
        end = (self._endpoints or {}).get("end_session_endpoint")
        if end and self._tokens.get("refresh_token"):
            with contextlib.suppress(httpx.HTTPError):  # signing out locally still works
                httpx.post(end, data={**self._client_params(), "refresh_token": self._tokens["refresh_token"]},
                           verify=self.verify, timeout=10)
        self._tokens, self._expires_at = {}, 0.0
        if self.token_cache and self.token_cache.exists():
            self.token_cache.unlink()

    # -------------------------------------------------------------- optional cache (--remember)
    def _save_cache(self) -> None:
        if not self.token_cache:
            return
        if not self._tokens:
            if self.token_cache.exists():
                self.token_cache.unlink()
            return
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        data = {"issuer": self.issuer, "client_id": self.client_id, "tokens": self._tokens,
                "expires_at": self._expires_at}
        fd = os.open(self.token_cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.token_cache.read_text())
        except (OSError, ValueError):
            return
        if data.get("issuer") == self.issuer and data.get("client_id") == self.client_id:
            self._tokens, self._expires_at = data.get("tokens", {}), float(data.get("expires_at", 0))


class KeycloakAuth(httpx.Auth):
    """httpx auth for the MCP connection: fresh Bearer token on every request, one retry after a 401."""

    def __init__(self, session: KeycloakSession):
        self.session = session

    def sync_auth_flow(self, request):
        token = self.session.access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self.session.force_refresh(token)}"
            yield request

    async def async_auth_flow(self, request):
        token = await anyio.to_thread.run_sync(self.session.access_token)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            new = await anyio.to_thread.run_sync(self.session.force_refresh, token)
            request.headers["Authorization"] = f"Bearer {new}"
            yield request
