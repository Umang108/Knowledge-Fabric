"""A stand-in Keycloak realm for tests, served over real HTTP.

Implements the parts of Keycloak's OpenID Connect API that TCS Knowledge Fabric's MCP server and agent use, with
Keycloak's URL layout and access-token claims:

  GET  /realms/{realm}/.well-known/openid-configuration
  GET  /realms/{realm}/protocol/openid-connect/certs                 JWKS (RS256)
  POST /realms/{realm}/protocol/openid-connect/auth/device           device authorization grant
  POST /realms/{realm}/protocol/openid-connect/token                 password, refresh_token, device_code

Access tokens look like Keycloak's: iss, sub, aud (from the client's audience mapper), azp, typ "Bearer",
scope, preferred_username, email, name, exp/iat/jti. `mint()` builds tokens directly (also broken ones).
"""

import secrets
import socket
import threading
import time
import uuid

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse

REALM = "graphbase"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    """Run an ASGI app with uvicorn in a background thread (a real TCP server)."""

    def __init__(self, app, port: int | None = None):
        self.port = port or free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class MockKeycloak:
    def __init__(self, users: dict[str, str] | None = None, token_lifetime: int = 300):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex
        self.users = users or {}
        self.clients = {
            # the agent: public client, device flow + direct grants, audience mapper adds graphbase-mcp
            "graphbase-agent": {"audience": ["graphbase-mcp", "account"], "secret": None},
            # the web app's client: its tokens are NOT meant for the MCP server
            "graphbase-app": {"audience": ["graphbase-app", "account"], "secret": None},
        }
        self.token_lifetime = token_lifetime
        self.device: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.token_requests: list[dict] = []
        self.port = free_port()
        self.app = self._build()

    # -------------------------------------------------------------- tokens
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/realms/{REALM}"

    def mint(self, username: str, client_id: str = "graphbase-agent", key=None, **overrides) -> str:
        now = int(time.time())
        claims = {
            "exp": now + self.token_lifetime,
            "iat": now,
            "jti": uuid.uuid4().hex,
            "iss": self.issuer,
            "aud": self.clients.get(client_id, {}).get("audience", ["account"]),
            "sub": str(uuid.uuid5(uuid.NAMESPACE_DNS, username)),
            "typ": "Bearer",
            "azp": client_id,
            "scope": "openid profile email",
            "preferred_username": username,
            "email": f"{username}@example.test",
            "name": username.replace(".", " ").title(),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": self.kid})

    def _tokens(self, username: str, client_id: str, scope: str | None = None) -> dict:
        refresh = secrets.token_urlsafe(24)
        self.refresh[refresh] = {"username": username, "client_id": client_id}
        extra = {"scope": scope} if scope else {}
        return {
            "access_token": self.mint(username, client_id, **extra),
            "expires_in": self.token_lifetime,
            "refresh_expires_in": 1800,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "id_token": self.mint(username, client_id, typ="ID", aud=client_id),
            "scope": scope or "openid profile email",
        }

    def approve(self, user_code: str, username: str) -> None:
        """What the person does in the browser for the device flow."""
        for d in self.device.values():
            if d["user_code"] == user_code:
                d["username"] = username
                return
        raise KeyError(user_code)

    # -------------------------------------------------------------- HTTP
    def _build(self) -> FastAPI:
        app = FastAPI()
        base = f"/realms/{REALM}"
        oidc = f"{base}/protocol/openid-connect"

        @app.get(f"{base}/.well-known/openid-configuration")
        def discovery():
            return {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
                "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
                "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs",
                "device_authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth/device",
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                    "password",
                    "urn:ietf:params:oauth:grant-type:device_code",
                ],
            }

        @app.get(f"{oidc}/certs")
        def certs():
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

        @app.post(f"{oidc}/auth/device")
        def device(client_id: str = Form(...), scope: str = Form("openid")):
            if client_id not in self.clients:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            code, user_code = (
                secrets.token_urlsafe(24),
                f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}",
            )
            self.device[code] = {
                "user_code": user_code,
                "client_id": client_id,
                "username": None,
                "expires": time.time() + 600,
            }
            return {
                "device_code": code,
                "user_code": user_code,
                "verification_uri": f"{self.issuer}/device",
                "verification_uri_complete": f"{self.issuer}/device?user_code={user_code}",
                "expires_in": 600,
                "interval": 1,
            }

        @app.post(f"{oidc}/token")
        def token(
            grant_type: str = Form(...),
            client_id: str = Form(...),
            username: str | None = Form(None),
            password: str | None = Form(None),
            refresh_token: str | None = Form(None),
            device_code: str | None = Form(None),
            scope: str | None = Form(None),
        ):
            self.token_requests.append({"grant_type": grant_type, "client_id": client_id})
            if client_id not in self.clients:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            if grant_type == "password":
                if self.users.get(username) != password:
                    return JSONResponse(
                        {"error": "invalid_grant", "error_description": "Invalid user credentials"}, status_code=401
                    )
                return self._tokens(username, client_id, scope)
            if grant_type == "refresh_token":
                info = self.refresh.pop(refresh_token or "", None)
                if not info or info["client_id"] != client_id:
                    return JSONResponse(
                        {"error": "invalid_grant", "error_description": "Invalid refresh token"}, status_code=400
                    )
                return self._tokens(info["username"], client_id)
            if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
                d = self.device.get(device_code or "")
                if not d or d["client_id"] != client_id or time.time() > d["expires"]:
                    return JSONResponse({"error": "expired_token"}, status_code=400)
                if not d["username"]:
                    return JSONResponse({"error": "authorization_pending"}, status_code=400)
                self.device.pop(device_code)
                return self._tokens(d["username"], client_id)
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        return app

    def serve(self) -> ServerThread:
        return ServerThread(self.app, self.port)
