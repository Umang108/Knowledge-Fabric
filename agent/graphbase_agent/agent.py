"""The Graphbase agent: a LangGraph ReAct agent whose only tools are the Graphbase MCP server's tools.

The MCP connection carries the signed-in person's Keycloak token, so the agent can only reach the knowledge
bases that person may use; the MCP server enforces it on every call.
"""

import os
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from graphbase_agent.keycloak import KeycloakAuth, KeycloakSession

SYSTEM_PROMPT = """You are the Graphbase assistant. You answer questions using the user's Graphbase knowledge
bases, through the tools provided, and nothing else.
- If you don't know which knowledge base to use, call list_knowledge_bases first.
- For a knowledge graph, call describe_knowledge_base to learn its node types and relationships, then use
  ask_knowledge_base for plain-language questions or query_graph for exact read-only Cypher.
- For a RAG store, use search_documents (or ask_knowledge_base) and quote the passages you rely on.
- Say which knowledge base an answer came from. If the tools return nothing relevant, say so; never guess.
- Properties marked as PII hold personal data: only include them when the question needs them."""


def get_llm() -> BaseChatModel:
    """Same switch as the backend: LLM_PROVIDER=azure (GPT-4.1) or ollama. The model must support tool calls."""
    provider = os.getenv("LLM_PROVIDER", "azure").lower()
    if provider == "azure":
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
            azure_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4.1"),
            temperature=0,
        )
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:7b-instruct"),
            temperature=0,
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")


@dataclass
class GraphbaseAgent:
    session: KeycloakSession
    mcp_url: str
    llm: BaseChatModel
    verify: bool | str = True
    history: list = field(default_factory=list)
    _graph: object = None

    async def _ensure(self):
        if self._graph is None:
            connection = {"transport": "streamable_http", "url": self.mcp_url, "auth": KeycloakAuth(self.session)}
            if self.verify is not True:
                import httpx

                connection["httpx_client_factory"] = lambda **kw: httpx.AsyncClient(verify=self.verify, **kw)
            client = MultiServerMCPClient({"graphbase": connection})
            self.tools = await client.get_tools()
            self._graph = create_react_agent(self.llm, self.tools, prompt=SYSTEM_PROMPT)
        return self._graph

    async def ask(self, question: str) -> str:
        graph = await self._ensure()
        self.history.append(HumanMessage(question))
        state = await graph.ainvoke({"messages": self.history}, config={"recursion_limit": 25})
        self.history = list(state["messages"])[-30:]
        answer = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage) and m.content), None)
        return answer.content if answer else "(no answer)"
