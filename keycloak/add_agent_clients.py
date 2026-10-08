"""Add (or repair) the Keycloak clients the MCP server and the agent need, in an existing realm.

Keycloak only imports graphbase-realm.json when the realm doesn't exist yet, so a realm created before the MCP
work has no `graphbase-agent` client; signing in then fails with
"Invalid client or Invalid client credentials". This script creates the two clients from graphbase-realm.json
through the admin REST API (standard library only):

    python keycloak/add_agent_clients.py --url http://10.138.77.117:8080
    python keycloak/add_agent_clients.py --url ... --realm graphbase --admin admin --test-user priya.nair

It is safe to run again: existing clients are updated to the settings in the file (their id is kept).
"""

import argparse
import base64
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENTS = ("graphbase-mcp", "graphbase-agent")
REALM_FILE = Path(__file__).with_name("graphbase-realm.json")


def http(method: str, url: str, token: str | None = None, body=None, form: dict | None = None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="Keycloak base URL, e.g. http://10.138.77.117:8080")
    p.add_argument("--realm", default="graphbase")
    p.add_argument("--admin", default="admin", help="Keycloak admin user")
    p.add_argument("--admin-password", help="asked if not given")
    p.add_argument("--admin-realm", default="master")
    p.add_argument("--test-user", help="after the fix, sign this user in through graphbase-agent and show the token")
    p.add_argument("--test-password", help="asked if not given")
    a = p.parse_args()
    base = a.url.rstrip("/")

    defs = {c["clientId"]: c for c in json.loads(REALM_FILE.read_text())["clients"] if c["clientId"] in CLIENTS}
    missing = set(CLIENTS) - set(defs)
    if missing:
        sys.exit(f"{REALM_FILE} has no definition for {', '.join(missing)}; use the updated realm file")

    status, body = http(
        "POST",
        f"{base}/realms/{a.admin_realm}/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": a.admin,
            "password": a.admin_password or getpass.getpass(f"Password for Keycloak admin '{a.admin}': "),
        },
    )
    if status != 200:
        sys.exit(f"Admin sign-in failed ({status}): {body}")
    admin = body["access_token"]
    api = f"{base}/admin/realms/{a.realm}"

    status, body = http("GET", api, admin)
    if status != 200:
        sys.exit(f"Realm '{a.realm}' not reachable ({status}): {body}")

    for client_id in CLIENTS:
        want = dict(defs[client_id])
        mappers = want.pop("protocolMappers", [])
        status, found = http("GET", f"{api}/clients?clientId={urllib.parse.quote(client_id)}", admin)
        if status != 200:
            sys.exit(f"Listing clients failed ({status}): {found}")
        if found:
            cid = found[0]["id"]
            merged = {
                **found[0],
                **want,
                "attributes": {**found[0].get("attributes", {}), **want.get("attributes", {})},
            }
            status, body = http("PUT", f"{api}/clients/{cid}", admin, merged)
            action = "updated"
        else:
            status, body = http("POST", f"{api}/clients", admin, want)
            action = "created"
            if status == 201:
                cid = http("GET", f"{api}/clients?clientId={urllib.parse.quote(client_id)}", admin)[1][0]["id"]
        if status not in (201, 204):
            sys.exit(f"Could not save client {client_id} ({status}): {body}")
        _, existing = http("GET", f"{api}/clients/{cid}/protocol-mappers/models", admin)
        names = {m["name"] for m in existing or []}
        for m in mappers:
            if m["name"] not in names:
                status, body = http("POST", f"{api}/clients/{cid}/protocol-mappers/models", admin, m)
                if status != 201:
                    sys.exit(f"Could not add mapper {m['name']} to {client_id} ({status}): {body}")
        print(f"{client_id}: {action}")

    if a.test_user:
        status, body = http(
            "POST",
            f"{base}/realms/{a.realm}/protocol/openid-connect/token",
            form={
                "grant_type": "password",
                "client_id": "graphbase-agent",
                "username": a.test_user,
                "password": a.test_password or getpass.getpass(f"Password for {a.test_user}: "),
                "scope": "openid",
            },
        )
        if status != 200:
            sys.exit(f"Test sign-in failed ({status}): {body}")
        c = claims(body["access_token"])
        print(f"Test sign-in OK: iss={c.get('iss')} aud={c.get('aud')} user={c.get('preferred_username')}")
        print("The MCP server's KEYCLOAK_URL must make exactly this iss: <KEYCLOAK_URL>/realms/<KEYCLOAK_REALM>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
