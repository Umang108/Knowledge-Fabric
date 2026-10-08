from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "graphbase"
    postgres_user: str = "postgres"
    postgres_password: str = ""

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_mode: str = "single"  # single | multi

    # RAG vector store: TurboQuant indexes (app/vectorstore.py). The backend and the MCP server must use the same
    # folder. Default: backend/data/vectors.
    vector_dir: str = str(Path(__file__).resolve().parents[1] / "data" / "vectors")
    turboquant_bits: int = 4  # bits per coordinate for new indexes: 4 (most accurate), 3 or 2 (smallest)

    llm_provider: str = "ollama"  # ollama | azure
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen2.5:7b-instruct"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_num_ctx: int = 8192
    llm_timeout_seconds: int = 600
    full_sheet_llm_analysis: bool = False
    max_llm_rows: int = 500
    max_llm_chars: int = 120_000
    # Chat guardrails (app/guardrails.py)
    guardrail_mask_pii: str = "high"  # mask PII at/above this NIST impact level: none | low | moderate | high
    guardrail_min_relevance: float = 0.0  # documents: below this best-passage score answer "not found" (0 = off)
    guardrail_llm_check: bool = False  # extra model-based screening of questions (adds one LLM call)

    # RAGAS evaluation (python -m app.cli eval-rag; app/eval_rag.py). Empty = use the app's own chat / embedding
    # model as the judge. A stronger judge than the model under test gives more reliable scores.
    ragas_judge_model: str = ""  # Azure deployment name or Ollama model
    ragas_judge_embed_model: str = ""
    ragas_report_dir: str = str(Path(__file__).resolve().parents[1] / "reports" / "ragas")

    # Langfuse tracing (app/observability.py); off unless both keys are set
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"  # EU cloud; https://us.cloud.langfuse.com for US
    langfuse_environment: str = ""  # e.g. dev / prod, to separate traces in one project
    langfuse_timeout: int = 20  # seconds per export (runs in the background, never in a request)
    langfuse_flush_at: int = 50  # spans per export batch
    langfuse_flush_interval: float = 2.0  # seconds between background exports
    langfuse_max_field_chars: int = 10_000  # longer prompt/answer text is trimmed in traces
    langfuse_capture_content: bool = True  # false: send timings, models and token usage only, no prompt text

    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str = "gpt-4.1"
    azure_openai_embed_deployment: str = "text-embedding-3-large"

    auth_provider: str = "local"  # local | keycloak
    # Encrypts the Keycloak tokens stored in sessions. JWT_SECRET is accepted as the old name.
    secret_key: str = Field("", validation_alias=AliasChoices("SECRET_KEY", "JWT_SECRET"))
    session_idle_minutes: int = 60
    session_max_hours: int = 12
    session_cookie_name: str = "graphbase_session"
    session_cookie_secure: bool = False  # set true when the app is served over https
    app_url: str = ""  # browser-facing URL of the app, e.g. http://graphbase.example:5173
    keycloak_url: str = ""  # browser-facing; token issuer is {keycloak_url}/realms/{realm}
    keycloak_internal_url: str = ""  # where the backend fetches signing keys; defaults to keycloak_url
    keycloak_realm: str = ""
    keycloak_client_id: str = ""
    keycloak_client_secret: str = ""

    upload_dir: str = "/uploads"
    # Origins (comma-separated) allowed to call the API from another origin with the session cookie.
    # Empty = same origin only (the UI uses the Vite proxy). "*" is ignored on purpose.
    cors_origins: str = ""

    # Connectors (SAP, ServiceNow). Hosts a connection may point at, comma-separated, wildcards allowed
    # (e.g. "*.service-now.com,s4.example.com"); empty = any host. Set it in production.
    connector_allowed_hosts: str = ""
    connector_ca_bundle: str = ""  # CA file for source systems with an internal certificate authority

    # MCP server (app/mcp_server.py). Agents call it with a Keycloak access token (Bearer); the token's
    # preferred_username is the TCS Knowledge Fabric user, so every tool sees exactly the KBs that user may access.
    mcp_host: str = "localhost"
    mcp_port: int = 8100
    mcp_public_url: str = "http://localhost:8100"  # how agents reach the MCP server (no /mcp suffix)
    mcp_audience: str = "graphbase-mcp"  # must be in the token's "aud" (Keycloak audience mapper)
    mcp_required_scopes: str = ""  # comma-separated scopes every token must carry, e.g. "graphbase"
    mcp_auto_provision_users: bool = True  # create the users row on a Keycloak user's first MCP call
    mcp_max_rows: int = 200  # cap on rows returned by query_graph

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{quote(self.postgres_user, safe='')}:{quote(self.postgres_password, safe='')}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
