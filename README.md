# Graphbase

Knowledge Graph + RAG studio: upload spreadsheets to build a reviewed Neo4j knowledge graph, or documents
to build a ChromaDB RAG store, share them with colleagues, and chat with them. See `CLAUDE.md` for the
spec and the decisions taken.

## Run

```bash
cp .env.example .env        # then set passwords / secrets
docker compose up -d --build
docker compose exec backend python -m app.cli seed-demo-data   # optional: the mockup knowledge bases
```

| Service  | URL |
|----------|-----|
| App      | http://localhost:5173 (demo users below) |
| API docs | http://localhost:8000/docs |
| Neo4j    | http://localhost:7474 (`neo4j` / password from `.env`) |
| Postgres | localhost:5433 |
| Chroma   | localhost:8001 |
| Keycloak | http://localhost:8080 (admin / admin), only with the `keycloak` profile |

Ollama runs on the host at `localhost:11434`. The `ollama-proxy` profile (on by default via
`COMPOSE_PROFILES`) forwards the Docker bridge address `172.17.0.1:11435` to it, so the backend uses
`http://host.docker.internal:11435`. On Docker Desktop, or if Ollama listens on `0.0.0.0`, drop the
profile and use `http://host.docker.internal:11434`. Pull the models first:
`ollama pull qwen2.5:3b` (or `qwen2.5:7b-instruct`) and `ollama pull nomic-embed-text`.

## Users (no sign-up screen)

```bash
docker compose exec backend python -m app.cli seed-demo-users        # local testing, password test1234
docker compose exec backend python -m app.cli create-user meera.s --name "Meera S"
docker compose exec backend python -m app.cli create-user meera.s --name "Meera S" --keycloak  # pre-provision
docker compose exec backend python -m app.cli deactivate-user meera.s
```

Demo users: `priya.nair`, `arjun.mehta`, `sneha.iyer`, `karthik.r`, `meera.s`, `rohan.d` (password
`test1234`). With `AUTH_PROVIDER=keycloak`, users are created automatically on first sign-in;
pre-provisioning only lets an owner grant access to someone who hasn't signed in yet.

`seed-demo-data` builds, from `data/samples`, the knowledge bases in the mockups:
`retail_supply_chain_kg` (priya owner; arjun, sneha, karthik users), `finance_ledger_kg` (arjun owner,
priya user), `retail_policies_rag` (priya owner) and `supplier_orders_review_kg` (a draft waiting on
the Review screen). It uses reviewed schemas, so it doesn't wait for LLM extraction; uploads through the
UI use the LLM.

## How it works

- **Sessions are server-side.** Sign-in (local password or Keycloak) is completed by the backend, which stores the
  session in Postgres and gives the browser only an HttpOnly cookie. The browser never holds a token. Sessions expire
  after `SESSION_IDLE_MINUTES` of inactivity or `SESSION_MAX_HOURS`; sign-out revokes the session (and the Keycloak
  session). State-changing API calls must carry the `X-Requested-With: graphbase` header (CSRF protection).

- **Graph extraction** (`app/extraction.py`): the LLM decides what each sheet's rows are, their keys,
  embedded entities, relationship names/directions and PII columns. Deterministic profiling supplies
  evidence (value overlap between sheets, line-level columns, junk columns) and checks every answer, so
  a small model still produces a correct, editable draft. Nothing is written to Neo4j until Submit.
- **Cypher** is generated from the schema (`app/graph_schema.py`), so the Review preview is exactly what
  runs. Rows are validated before writing (`app/loader.py`): a row is rejected and reported if a key is
  missing or a referenced entity doesn't exist; nodes merge on their key property.
- **Neo4j modes** (`app/graphstore.py`): `NEO4J_MODE=single` (Community) keeps every KB in one database
  under a `KB_<name>` label and rewrites chat Cypher so each node pattern is scoped to it;
  `NEO4J_MODE=multi` (Enterprise) creates one database per KB. Chat Cypher is always checked to be
  read-only and runs in a read transaction.
- **Access** (`app/kb.py`) is checked on the server for every request; `kb_access` is the audit trail.
- **PII** (`kb_pii_fields`) is detected automatically for graph columns and RAG documents; raw values are
  never stored.

## PII review

Detected PII columns are listed on the Review screen under **Personal data (PII)**. The owner ticks or unticks
a column (also inside a node or relationship's Edit row), changes its category, or ticks *Show all stored
columns* to mark a column nobody detected. Each finding keeps a status (`detected`, `confirmed`, `dismissed`)
and its source (`llm`, `rules`, `user`); dismissed findings stay in `kb_pii_fields` for the audit trail.
The choices are saved with the schema on Submit.

## MCP server and agent (Keycloak)

`app/mcp_server.py` exposes the knowledge bases over MCP (streamable HTTP at `/mcp`, compose service `mcp`,
port `MCP_HOST_PORT`). Every request needs a Keycloak access token (`Authorization: Bearer`); the server
checks signature, issuer, audience (`MCP_AUDIENCE`), expiry and scopes, maps `preferred_username` to the
`users` row, and then applies the same KB access rules as the web app.

| Tool | What it does |
|---|---|
| `whoami` | The signed-in user |
| `list_knowledge_bases` | KBs the user owns or was granted |
| `describe_knowledge_base` | Node types, properties (with PII flags), relationships, counts / RAG documents |
| `ask_knowledge_base` | Question answered from the graph (generated Cypher) or the RAG store |
| `query_graph` | Read-only Cypher, scoped to the KB, capped at `MCP_MAX_ROWS` |
| `search_documents` | Top passages from a RAG store |

Keycloak setup (already in `keycloak/graphbase-realm.json` for the local container): a client
`graphbase-mcp` (the audience) and a public client `graphbase-agent` with *OAuth 2.0 Device Authorization Grant*
(and optionally *Direct access grants*) plus an **Audience** mapper that adds `graphbase-mcp` to the access token.

The agent (`agent/`) signs the user in with Keycloak and lets an LLM use those tools:

```bash
cd agent && pip install -r requirements.txt
python -m graphbase_agent                       # device login: open the URL, enter the code, then chat
python -m graphbase_agent --login password --username priya.nair "Which suppliers deliver to Chennai?"
```

Any other MCP client works too: point it at `<MCP_PUBLIC_URL>/mcp` with a Keycloak token; the protected-resource
metadata is at `/.well-known/oauth-protected-resource/mcp`.

## SAP and ServiceNow connectors

*Workspace -> Connected systems -> Manage connections* saves a connection (base URL, user + password or OAuth client
credentials; the secret is encrypted with `SECRET_KEY` and never returned). *Use* then lists the tables to pull:

- **ServiceNow** (Table API): preset *IT service management* (incident, problem, change_request, sys_user,
  sys_user_group, cmdb_ci) builds a graph; *Knowledge articles* (kb_knowledge, published) builds a RAG store.
  Reference fields become `<field>` (sys_id) + `<field>_name`; encoded-query filters are supported.
- **SAP** (OData V2 or V4, S/4HANA or BTP): presets *Sales* (business partners, sales orders and items, products)
  and *Procurement*. `sap-client`, `$select`, `$filter`, server paging and `/Date()/` values are handled.

The tables are written as a workbook in the KB's upload folder and go through the normal extraction and Review.
In connector-built graphs every relationship is optional and a reference to a record that wasn't pulled is
skipped (not the whole row). *Add data -> Or pull from a connected system* refreshes a ready KB, optionally only
records changed since a date (SAP datasets need their changed field, e.g. `LastChangeDateTime`). Set
`CONNECTOR_ALLOWED_HOSTS` in production.

## Tests

```bash
docker compose exec backend pytest -q                    # 202 tests (start the keycloak profile for the Keycloak ones)
docker compose exec backend pytest -m llm -s             # real-LLM quality on both datasets, ~1 h on CPU
.venv/bin/python -m pytest e2e -q                        # browser tests of every screen (needs seed-demo-data)
```

Backend tests use a separate `graphbase_test` database and `t_*` KB names. The benchmark writes
`backend/tests/reports/llm_quality.json` and `llm_hospital.json`. The browser tests save screenshots to `e2e/screenshots/`.

Local Python environment for the dataset generator and browser tests:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt -r data/requirements.txt playwright
.venv/bin/python -m playwright install chromium
```

## Test datasets

Two unrelated, deliberately messy datasets, each generated deterministically with its own ground truth:

- **Retail** (`data/generate_dataset.py` -> `data/samples/`, `data/expected/manifest.json`): suppliers, products,
  warehouses, customers, orders, inventory, a finance ledger and retail policy documents.
- **Hospital** (`data/generate_hospital_dataset.py` -> `data/samples/hospital/`, `data/expected/hospital_manifest.json`):
  departments, doctors (with supervisors), patients (with insurers), wards, procedures, medications, admissions (one
  row per procedure), prescriptions and clinical policy documents; includes a non-table ReadMe sheet, Indian lakh
  amounts, `05-Mar-2026` dates and circular references.


`data/generate_dataset.py` builds a deliberately messy, deterministic dataset in `data/samples/`
and the ground truth in `data/expected/manifest.json` (expected counts, rejected rows, PII columns,
and question/answer pairs). Regenerate with `.venv/bin/python data/generate_dataset.py`.

## Switching to production (work system)

- **Keycloak:** `AUTH_PROVIDER=keycloak`, `APP_URL` (the app's address as users open it), `KEYCLOAK_URL`
  (browser-facing, must match the token issuer), `KEYCLOAK_INTERNAL_URL` (reachable from the backend container),
  `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID` (and `KEYCLOAK_CLIENT_SECRET` for a confidential client). The client needs
  the standard flow, PKCE S256 and the redirect URI `<APP_URL>/api/auth/callback`; see `keycloak/graphbase-realm.json`.
  Remove `keycloak` from `COMPOSE_PROFILES`.
- **Neo4j Enterprise:** `NEO4J_IMAGE=neo4j:5-enterprise`, `NEO4J_ACCEPT_LICENSE=yes`, `NEO4J_MODE=multi`.
- **Model:** `OLLAMA_CHAT_MODEL=qwen2.5:7b-instruct` (or larger) if the machine has the memory/GPU.
- **Azure OpenAI:** uncomment the Azure blocks in `app/llm.py` and `langchain-openai` in
  `backend/requirements.txt`, then `LLM_PROVIDER=azure` and the `AZURE_OPENAI_*` variables.
