# Graphbase: Knowledge Graph + RAG Studio

Read this file fully before writing code. UI mockups are in `docs/mockups/*.png` (also `docs/Graphbase_UI_Mockups.pdf`, HTML source in `docs/mockup-html/`). Look at the PNGs and match the layout closely.

## Stack (fixed)
- Frontend: React (Vite)
- Backend: FastAPI (Python), LangChain / LangGraph
- Graph DB: Neo4j | Vector store: TurboQuant (turbovec index files + chunk text in Postgres) | Relational DB: Postgres
- Everything runs via Docker Compose
- LLM is swappable: **Ollama model is active**; **Azure OpenAI GPT-4.1 code is written but commented out**. Put both behind one factory (`get_llm()`, `get_embeddings()`) selected by an env var, so switching is a one-line change.

## Screens (see docs/mockups)
1. Login (user ID + password) -> `1_Main.png`
2. Workspace -> `2_Workspace.png`: left panel has RAG upload and Knowledge-graph upload (CSV/XLSX). Right main panel has the Create form: knowledge graph name, domain, sub-domain, Create button, plus a list of the user's knowledge bases with role (Owner/User).
3. Extraction running -> `3_Processing.png`
4. Review -> `4_Review.png`: shows LLM-extracted nodes/entities and relationships, each editable/deletable, live Cypher preview, Submit button. Nothing is written to Neo4j until Submit.
5. Manage access -> `5_Access.png` (owner only)
6. Add data -> `6_AddData.png`
7. Chat -> `7_Chat.png`
8. Postgres tables -> `8_Schema.png`

## Behaviour
**KG creation flow:** user uploads CSV/XLSX + fills name/domain/sub-domain -> backend runs in background: file goes to the LLM, which returns nodes, entities, relationships and Cypher -> status shown on screen 3 -> Review screen (4) lets user edit/delete -> on Submit, build the graph in Neo4j from the approved schema and store it.

**RAG flow:** upload PDF/DOCX/TXT -> chunk, embed, store in the TurboQuant index automatically in the background. No review screen.

**Ownership & access:** the user who creates a knowledge base becomes its owner. Only the owner can grant or revoke access to other users. Every grant/revoke is logged with who did it.

**Additional data pipeline:** for an existing KG, new CSV/XLSX is ingested using the already-approved schema (no review screen, merge on key properties, report rejected rows). For RAG bases, new docs are chunked and added to the same TurboQuant index.

**Chat:** user picks a knowledge base they own or have access to. Graph bases: LangGraph flow that generates Cypher, runs it on Neo4j, answers from results (show Cypher + path). RAG bases: retrieve from the TurboQuant index and answer. Enforce access on the backend for every query, never only in the UI.

## Postgres tables
`kb_access`: id, kb_name, user_id, role (owner|user), granted_by, granted_at, revoked_at (null while active). This is the audit trail.
`knowledge_bases`: user_id, kb_name, kb_type (graph|rag), domain, sub_domain, access (owner|user). One row per user per base they can reach.
Also a `users` table for login (hashed passwords).
Creating a KB inserts an owner row in both tables. Granting adds a row to each. Revoking sets revoked_at and removes the user row.

**Audit columns (manager request):** every table also has `created_at`, `updated_at` (timestamptz) and `modified_by` (user_id of whoever last changed the row; `system` for automated jobs). `updated_at` is maintained by a DB trigger; `modified_by` is set by the app. A revoke sets `modified_by` to the revoker.

`kb_pii_fields` (manager request): one row per PII property found in a knowledge base, detected **automatically by the LLM** during extraction (no user action needed). Columns: id, kb_name, node_label, property_name, pii_category (e.g. person_name, email, phone, address, date_of_birth, government_id, bank_account), sensitivity (high|medium|low), confidence, reason, detected_by (`llm`), plus the audit columns. Shown on the Review screen as a PII badge on the affected node type/property.
RAG documents are PII-scanned too (LLM over each document's chunks during ingest): those rows have `source_document` set and `node_label`/`property_name` null, with an occurrence count per category. Never store the raw PII value in this table.

Supporting tables (not in the mockups): `kb_catalog` (one row per KB: type, owner, status, storage location, draft + approved graph schema/Cypher reused by add-data; `kb_name` primary key so names are globally unique), `jobs` (extraction/ingest progress for screen 3), `pipeline_runs` (add-data runs + rejected-row reports). Screen 8 is database documentation only, not an app page.

## Decisions (answered 2026-09-29)
- "user" role can **add data and chat**. Only the owner grants/revokes.
- RAG and graph bases in **one list**.
- Ollama: chat `qwen2.5` (7b-instruct default for the work system; local testing uses `qwen2.5:3b` because this laptop has 7 GB RAM and no GPU; model name is an env var), embeddings `nomic-embed-text`.
- Auth: **Keycloak in production** (user's work system runs Keycloak). Local testing uses Postgres users + passwords. Both behind `AUTH_PROVIDER=local|keycloak`. **Authentication and sessions are handled entirely by the backend** (user request, 30 Sep): server-side sessions in the `sessions` table, the browser only gets an HttpOnly SameSite cookie with a random token (hash stored), idle + absolute expiry, logout revokes. For Keycloak the backend runs the OIDC authorization-code flow with PKCE (`/api/auth/login` -> Keycloak -> `/api/auth/callback`), verifies the ID token, keeps the refresh token encrypted (SECRET_KEY) and re-checks with Keycloak when the access token expires; `preferred_username` becomes `user_id` and the `users` row is created on first sign-in. CSRF: unsafe API methods require `X-Requested-With: graphbase`. No tokens in browser storage.
- Neo4j: local testing uses **Community (single database)**, but the code must also work on **Enterprise with many KBs in parallel**. `NEO4J_MODE=single|multi`: `multi` = one Neo4j database per KB; `single` = shared database, every node carries a per-KB label so KBs stay isolated. All graph access goes through one GraphStore abstraction; chat Cypher runs read-only and is scoped to the KB.
- KG extraction sends the LLM each sheet's headers + sample rows (not the whole file); the approved Cypher is `UNWIND $rows`-style and runs over all rows in batches.
- `kb_name` is globally unique. RAG bases are created from the same Create form (switches to RAG mode). Users are created by a CLI/seed script (no signup screen).
- (2026-09-30, user: "complete everything, test everything well") Phases 3-9 were built in one go; the stop-after-each-phase rule was waived for that request.

## Implementation notes (keep these when changing the code)
- The LLM proposes, evidence verifies: small models (qwen2.5:3b) list every column as "ignore", reuse labels, reverse directions and over-flag PII. `app/extraction.py` feeds the model data evidence (reference columns, unique columns) and validates every answer (junk-only ignores, text-like keys, link-table demotion, line-level columns moved to relationships, PII categories must match the values). Don't remove these checks when switching to a bigger model; they are cheap.
- Cypher for building graphs is generated from the reviewed schema (`app/graph_schema.py`), never taken verbatim from the LLM; the Review preview is the same code path.
- Load contract (`app/loader.py`, also in `data/generate_dataset.py`): keys compared strip+upper; a row is rejected if a key is missing or a referenced entity is unknown (cascading); last row wins; nodes before relationships.
- Lint: `ruff` (backend/ruff.toml) and ESLint (frontend/eslint.config.js) must stay clean.
- Chat Cypher is untrusted: `check_read_only` + `scope_cypher` (single mode) + read transaction + result limit.
- PII rows never contain raw values.
- PII review: findings carry `status` (detected|confirmed|dismissed) and `source` (llm|rules|user); `extraction.clean_pii` validates user edits; only `active_pii` (not dismissed) drives badges and `kb_pii_fields`. GUID/hex ID columns are never PII.
- MCP server (`app/mcp_server.py`, `app/mcp_auth.py`): Keycloak Bearer tokens only (issuer, audience `MCP_AUDIENCE`, expiry, scopes); every tool goes through `kb.require_access`. `query_graph` uses `GraphStore.run_readonly`, which refuses any node pattern without the KB label in single mode. Needs fastapi >= 0.120 (Starlette 1.x).
- Connectors (`app/connectors/`): tables become an .xlsx in the KB upload folder and reuse the normal pipelines; connector graphs are relaxed (`required: false`, `on_unknown: "skip"`). Secrets encrypted with `SECRET_KEY`, never returned by the API. Integer keys named after the entity (SAP `SalesOrder`) are valid keys.
- Langfuse (`app/observability.py`): use `trace_context` / `observe_span` / `observe_llm` only. Langfuse keeps its own OpenTelemetry TracerProvider (never the global one: FastAPI >= 0.120 would then export every HTTP request); only the root observation sets trace name/user/session; never flush inside a request or job.
- Product name shown to users: **TCS Knowledge Fabric**. Internal identifiers stay (`graphbase_agent` package, `graphbase-*` Keycloak clients, `GRAPHBASE_MCP_URL`, `graphbase_session` cookie, `X-Requested-With: graphbase`) so existing deployments keep working.
- Guardrails (`app/guardrails.py`) run inside `chat.graph_answer` / `chat.rag_answer` (so web and MCP share them); PII masking uses the NIST levels from `app/nist_pii.py`. Chat turns are saved per conversation (last 10 per user).
- KB names: `^[A-Za-z][A-Za-z0-9_]{2,62}$`, unique case-insensitively (`kb.validate_name` + unique index on lower()).
- Don't run `ruff format` on whole folders (teammate files are not ruff-formatted); format only files you create.

## Local environment
- Local Python: `uv` venv at `.venv` (Python 3.11) for the dataset generator and running tests/tools outside Docker.
- Local credentials: Postgres `postgres`/`postgres` (host port 5433; 5432 is taken by a host Postgres). Neo4j `neo4j`/`neo4j123` (Neo4j 5 refuses `neo4j` as the password). Test users' password `test1234`.
- Host Ollama listens on 127.0.0.1:11434 only; the `ollama-proxy` compose profile exposes it to containers at `host.docker.internal:11435`.

## Testing
- Two deliberately difficult, unrelated test datasets live in `data/` (retail: `data/generate_dataset.py`; hospital: `data/generate_hospital_dataset.py`), each with a deterministic seed and expected answers for automated checks. Every phase is tested against it; code ships to the work system only after tests pass.

## Working style
- Build in phases and stop after each for review: 1) docker-compose + project skeleton, 2) Postgres schema + auth, 3) LLM/embedding factory, 4) KG extraction + review API, 5) RAG ingest, 6) access control, 7) add-data pipeline, 8) chat, 9) React screens.
- Keep secrets in `.env` (provide `.env.example`). Never commit real keys.
