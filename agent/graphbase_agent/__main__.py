"""Command line for the Graphbase agent.

    python -m graphbase_agent                                 # sign in (device code), then chat
    python -m graphbase_agent "Which suppliers deliver to Chennai?"
    python -m graphbase_agent --login password --username priya.nair    # asks for the password
    python -m graphbase_agent --remember ...                  # keep the Keycloak session in ~/.graphbase

Settings come from the environment (or a .env file in the current or parent folder):
    GRAPHBASE_MCP_URL     e.g. http://10.138.77.117:8100/mcp
    KEYCLOAK_URL          e.g. https://keycloak.example.com   (browser-facing URL, the token issuer)
    KEYCLOAK_REALM        e.g. graphbase
    AGENT_CLIENT_ID       Keycloak client for the agent (default graphbase-agent)
    AGENT_CLIENT_SECRET   only for a confidential client
    AGENT_SCOPE           default "openid"
    KEYCLOAK_CA_BUNDLE    CA file if Keycloak/MCP use an internal certificate authority
    LLM_PROVIDER + AZURE_OPENAI_* or OLLAMA_*   the model that drives the agent (same names as the backend)
"""

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from graphbase_agent import agent as agent_mod
from graphbase_agent.keycloak import KeycloakSession, LoginError


def _load_dotenv() -> None:
    for folder in (Path.cwd(), *Path.cwd().parents[:2]):
        env = folder / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def _setting(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if not value:
        sys.exit(f"Set {name} (environment or .env)")
    return value


def main(argv: list[str] | None = None, input_fn=input, out=print) -> int:
    p = argparse.ArgumentParser(prog="python -m graphbase_agent", description="Ask your Graphbase knowledge bases.")
    p.add_argument("question", nargs="*", help="ask one question and exit (otherwise interactive)")
    p.add_argument("--login", choices=["device", "password"], default="device")
    p.add_argument("--username")
    p.add_argument("--password", help="for scripts; prefer being asked")
    p.add_argument("--remember", action="store_true", help="keep the Keycloak session in ~/.graphbase/")
    p.add_argument("--logout", action="store_true", help="end the remembered session and exit")
    a = p.parse_args(argv)
    _load_dotenv()

    verify = os.getenv("KEYCLOAK_CA_BUNDLE") or True
    session = KeycloakSession(
        _setting("KEYCLOAK_URL"),
        _setting("KEYCLOAK_REALM"),
        os.getenv("AGENT_CLIENT_ID", "graphbase-agent"),
        os.getenv("AGENT_CLIENT_SECRET") or None,
        scope=os.getenv("AGENT_SCOPE", "openid"),
        verify=verify,
        token_cache=Path.home() / ".graphbase" / "agent-session.json" if (a.remember or a.logout) else None,
    )
    if a.logout:
        session.logout()
        out("Signed out.")
        return 0
    try:
        if session.signed_in:
            session.access_token()  # refreshes a remembered session, or fails if it has ended
        elif a.login == "password":
            username = a.username or input_fn("Keycloak username: ")
            session.login_password(username, a.password or getpass.getpass("Password: "))
        else:
            session.login_device(show=lambda msg, *_: out(msg))
    except LoginError as exc:
        out(f"Sign-in failed: {exc}")
        return 2
    out(f"Signed in as {session.user['user_id']}.")

    bot = agent_mod.GraphbaseAgent(session, _setting("GRAPHBASE_MCP_URL"), agent_mod.get_llm(), verify=verify)

    async def run() -> int:
        if a.question:
            out(await bot.ask(" ".join(a.question)))
            return 0
        out("Ask a question (empty line or 'exit' to quit).")
        while True:
            try:
                q = input_fn("> ").strip()
            except EOFError:
                return 0
            if q.lower() in ("", "exit", "quit"):
                return 0
            try:
                out(await bot.ask(q))
            except LoginError as exc:
                out(f"Sign-in needed: {exc}")
                return 2

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
