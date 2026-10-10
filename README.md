# TCS Knowledge Fabric

Knowledge Graph + RAG studio: upload spreadsheets to build a reviewed Neo4j knowledge graph, or documents
to build a RAG store (TurboQuant vector index), share them with colleagues, and chat with them. See `CLAUDE.md` for the
spec and the decisions taken.

## Run

```bash
cp .env.example .env        # then set passwords / secrets
docker compose up -d --build
docker compose exec backend python -m app.cli seed-demo-data   # optional: the mockup knowledge bases
```

## Configuration

All operational settings are loaded by the typed `Settings` model in
[`backend/app/config.py`](backend/app/config.py). Set them in the repository root
`.env`; [`.env.example`](.env.example) is the complete reference grouped by
application, databases, TurboQuant, LLMs, security, connectors, and evaluation.
Changing these values does not require source-code edits. Existing defaults are
kept for local development, so older `.env` files remain compatible.

| Service  | URL |
|----------|-----|
| App      | http://localhost:5173 (demo users below) |
| API docs | http://localhost:8000/docs |
| Neo4j    | http://localhost:7474 (`neo4j` / password from `.env`) |
| Postgres | localhost:5433 |
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
- **RAG vector store** (`app/vectorstore.py`): Google's TurboQuant vector quantization through the `turbovec`
  library. Each document knowledge base is one index file, `VECTOR_DIR/<kb_name>.tvim`, holding every chunk's
  embedding compressed to `TURBOQUANT_BITS` (default 4) bits per coordinate; the chunk text and metadata are in the
  Postgres table `rag_chunks`. There is no vector database server: the backend and the MCP server load the index
  files themselves, so both must use the same `VECTOR_DIR` (default `backend/data/vectors`; the `vectors` volume in
  Docker Compose). Search is cosine similarity, as before. Back up `VECTOR_DIR` together with Postgres.
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

## Checking the setup

`python -m app.cli doctor` (from `backend/`, with the same `.env` as uvicorn) checks Postgres, the upload folder,
the TurboQuant vector store (a real save, search and delete), the embedding model, the chat model and Neo4j, and says
what to fix. RAG errors name the failing service, e.g. "Vector store (TurboQuant index in /path) failed ... Hint: ...".

All document knowledge bases use TurboQuant. If a knowledge base has no documents, upload them again through the
application so they are chunked, embedded, and saved to its TurboQuant index.

## RAGAS evaluation (TurboQuant RAG and graph responses)

`python -m app.cli eval-ragas` measures how well either knowledge-base type answers. Every question in a test set is
answered exactly as in the chat, then [RAGAS](https://docs.ragas.io) scores the answer. RAG knowledge bases use the
TurboQuant index for retrieval; graph knowledge bases use the read-only, KB-scoped graph chat flow. ChromaDB is not
used:

| Metric | What it checks (0-1, higher is better) | Needs a reference answer |
|---|---|---|
| faithfulness | every claim in the answer is supported by the retrieved passages (no hallucination) | no |
| answer_relevancy | the answer addresses the question | no |
| context_precision | the useful passages are ranked first | no (uses it when given) |
| context_recall | the passages contain everything the reference answer needs | yes |
| factual_correctness | the answer agrees with the reference answer | yes |

```bash
cd backend
uv sync                                  # installs the application and RAGAS dependencies from pyproject.toml
python -m app.cli eval-ragas --kb retail_policies_rag --testset eval/testsets/retail_policies_rag.csv
python -m app.cli eval-ragas --kb retail_supply_chain_kg --testset my_graph_questions.csv
python -m app.cli eval-ragas --kb my_kb --testset my_questions.csv --metrics faithfulness,context_recall --limit 20
python -m app.cli eval-ragas --kb my_kb --testset my_questions.csv --min-score 0.7   # exit code 1 below 0.7 (CI)
```

`eval-rag`, `eval-graph`, and `eval-kb` remain accepted aliases. Graph result rows are passed to RAGAS as the
retrieved contexts, while the report retains the generated Cypher and returned rows for debugging.

- **Test set:** CSV with `id,question,reference` (see `eval/testsets/TEMPLATE.csv`), or JSONL / JSON with the same
  keys. Write the reference as a full sentence, the way the documents state it. Without a reference only the first
  three metrics run. Samples: `eval/testsets/retail_policies_rag.csv`, `hospital_rag.csv`.
- **Judge:** `RAGAS_JUDGE_MODEL` / `RAGAS_JUDGE_EMBED_MODEL` (or `--judge-model`) choose a different Azure deployment
  or Ollama model. A stronger judge than the model under test gives more reliable scores. Each question costs
  roughly 10-20 judge calls.
- **Docker:** `docker compose exec backend sh -c "uv sync && python -m app.cli eval-ragas
  --kb retail_policies_rag --testset eval/testsets/retail_policies_rag.csv"`; reports appear in `backend/reports/ragas`.
- **Results are saved three ways:**
  1. a folder per run, `backend/reports/ragas/<kb>_<time>/` (`RAGAS_REPORT_DIR`): `results.csv` (opens in Excel:
     each question, the answer, sources, every score and why a score is missing), `results.jsonl` (also the full
     passages) and `summary.json` (averages, models and settings, change since the previous run);
  2. Postgres: `rag_eval_runs` (one row per run, average scores) and `rag_eval_results` (one row per question);
  3. Langfuse, when tracing is on: each answer is a "RAG Evaluation" trace with `ragas_<metric>` scores.

```sql
SELECT id, started_at, metrics FROM rag_eval_runs WHERE kb_name = 'retail_policies_rag' ORDER BY started_at;
```

## Chat: conversations, guardrails and prompts

- **Saved conversations**: every chat is saved; each user keeps the last 10 (older ones are removed when a new one
  starts). The Chat screen lists them on the left; follow-up questions use the saved turns as history. Tables
  `chat_conversations` and `chat_messages`; a conversation is hidden when the user loses access to its knowledge base.
- **Guardrails** (`app/guardrails.py`, web chat and MCP tools): questions that try prompt injection, ask to change
  data, ask for system credentials or are clearly harmful are answered with a refusal and never reach the model.
  Values of PII whose NIST impact is at or above `GUARDRAIL_MASK_PII` (default high: government IDs, financial
  accounts, health, biometrics) are masked in query results before the model sees them, and in answers and
  document passages by pattern. `GUARDRAIL_MIN_RELEVANCE` stops document answers built on unrelated passages. The
  UI shows which guardrail acted under each answer.
- **Prompts** (`app/chat.py`): the Cypher planner declines questions the graph can't answer instead of guessing;
  answers start with the direct answer, keep values exact, use lists for many items and never reveal masked values;
  document answers cite each fact and say plainly when the documents don't contain the answer.

## PII classification (NIST)

`app/nist_pii.py` follows NIST SP 800-122: each PII column is a direct identifier or linked/linkable information
and gets a confidentiality impact level (Low / Moderate / High) from its field sensitivity, what it is stored with
(a name next to a bank account is more sensitive; a date of birth with no identifier in the same records is less),
and how many records hold it. Each finding lists the factors and the NIST Privacy Framework safeguards for its level
(ID.IM-P inventory, PR.AC-P access control, PR.DS-P data security, CT.DP-P disassociated processing). Shown on the
Review screen and stored in `kb_pii_fields` (`nist_identifier`, `nist_impact`, `nist_factors`).

## Sessions

Sessions are server-side (see How it works). The **Sessions** page lists where the user is signed in (browser, IP,
last activity, end time) and signs out one or all other sessions. Two minutes before an idle timeout the app asks
"Stay signed in?"; an ended session lands on the sign-in page with a notice. `SESSION_IDLE_MINUTES`,
`SESSION_MAX_HOURS`.

## Knowledge base names

3-63 characters, letters (upper or lower case), digits and underscores, starting with a letter. Names are unique
regardless of case (`Sales_KG` and `sales_kg` can't both exist; Neo4j database names are case-insensitive).

## Langfuse tracing

Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` (see `.env.example`) for the backend and
the MCP server. Each user operation becomes one trace, named after the operation (`Chat`, `RAG Chat`,
`KG Extraction`, `KG Build`, `Embedding`, `Add Data`, `MCP <tool>`), with the user, a session (web session, job or
Keycloak session) and every model call, embedding, retrieval and Neo4j query inside it, including token usage.
Spans are sent in the background every `LANGFUSE_FLUSH_INTERVAL` seconds, so a trace appears a few seconds after the
operation ends. `app/observability.py` explains the design rules (private OpenTelemetry provider, trace identity
set only by the root, no flush inside requests, trimmed payloads); `tests/test_observability.py` checks them
against a stand-in Langfuse endpoint.

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
docker compose exec backend pytest -q                    # ~310 tests (start the keycloak profile for the Keycloak ones)
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
