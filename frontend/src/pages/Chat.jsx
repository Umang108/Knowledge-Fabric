import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, formatDate } from "../api.js";
import {
  CopyButton,
  CypherPanel,
  ErrorBox,
  TypeBadge,
} from "../components/common.jsx";

function Guardrails({ items }) {
  if (!items?.length) return null;

  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {items.map((g, i) => (
        <span key={i} className="guard" title={`Guardrail: ${g.rule}`}>
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            aria-hidden="true"
          >
            <path d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5z" />
          </svg>

          {g.rule === "pii_masked"
            ? `Privacy: ${g.detail}`
            : g.rule === "grounding"
              ? `Grounding: ${g.detail}`
              : "Blocked by a guardrail"}
        </span>
      ))}
    </div>
  );
}

function QueryProgress({ type, step }) {
  const stages =
    type === "graph"
      ? [
          "Understanding your question",
          "Building a scoped Cypher query",
          "Reading the knowledge graph",
          "Preparing the answer",
        ]
      : [
          "Understanding your question",
          "Searching relevant passages",
          "Checking source context",
          "Preparing the answer",
        ];

  return (
    <div className="query-progress" role="status" aria-live="polite">
      <div className="query-progress-head">
        <span className="query-spinner" aria-hidden="true" />

        <div>
          <strong>
            {type === "graph"
              ? "Working through the graph"
              : "Searching your knowledge base"}
          </strong>

          <div className="muted small">This usually takes a few seconds</div>
        </div>
      </div>

      <div className="query-steps">
        {stages.map((label, i) => (
          <div
            className={`query-step${
              i < step ? " done" : i === step ? " active" : ""
            }`}
            key={label}
          >
            <span className="query-step-mark" aria-hidden="true">
              {i < step ? "✓" : i === step ? "" : i + 1}
            </span>

            <span>{label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Answer({ m }) {
  return (
    <div className="answer-block">
      <div className="answer-label">
        <span className="answer-avatar">KF</span>
        <span>Knowledge Fabric</span>
      </div>

      <div className="bubble bot">{m.answer}</div>

      <Guardrails items={m.guardrails} />

      {m.path?.length > 0 && (
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <span className="muted small">Graph path</span>

          {m.path.map((p, i) =>
            p.kind === "rel" ? (
              <span key={i} className="rel">
                {p.text}
              </span>
            ) : (
              <span key={i} className="chip">
                {p.text}
              </span>
            ),
          )}
        </div>
      )}

      {m.cypher && (
        <CypherPanel
          title="Cypher used"
          code={m.cypher}
          meta={m.row_count != null ? `${m.row_count} rows` : ""}
        />
      )}

      {m.sources?.length > 0 && (
        <div className="card" style={{ padding: "10px 14px" }}>
          <div className="section" style={{ marginBottom: 6 }}>
            Sources
          </div>

          {m.sources.slice(0, 4).map((s) => (
            <div key={s.n} className="small" style={{ padding: "4px 0" }}>
              <strong>
                [{s.n}] {s.source}
                {s.page ? `, page ${s.page}` : ""}
              </strong>

              <span className="muted">: {s.snippet.slice(0, 160)}…</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// "507 entities in 6 node types (Customer 120 · Order 300 · …)"
function Breakdown({ total, kinds, noun, kindNoun, by }) {
  const entries = Object.entries(by || {}).sort((a, b) => b[1] - a[1]);

  const shown = entries
    .slice(0, 6)
    .map(([k, n]) => `${k} ${n.toLocaleString()}`)
    .join(" · ");

  const more = entries.length > 6 ? ` · +${entries.length - 6} more` : "";

  return (
    <span
      title={entries.map(([k, n]) => `${k}: ${n.toLocaleString()}`).join("\n")}
    >
      <strong>
        {(total ?? 0).toLocaleString()} {noun}
      </strong>{" "}
      in {kinds} {kindNoun}
      {entries.length > 0 && ` (${shown}${more})`}
    </span>
  );
}

export default function Chat() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const [kbs, setKbs] = useState(null);
  const [kbOpen, setKbOpen] = useState(false);
  const [detail, setDetail] = useState(null);

  const [conversations, setConversations] = useState([]);

  const [convId, setConvId] = useState(null);

  const [thread, setThread] = useState([]);

  const [question, setQuestion] = useState("");

  const [pending, setPending] = useState(false);
  const [queryStep, setQueryStep] = useState(0);

  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);

  const bottom = useRef(null);
  const kbDropdown = useRef(null);

  // bumped when the user switches conversation:
  // late answers are dropped
  const generation = useRef(0);

  useEffect(() => {
    if (!kbOpen) return undefined;

    const closeOnOutsidePointer = (event) => {
      if (!kbDropdown.current?.contains(event.target)) {
        setKbOpen(false);
      }
    };

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () =>
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [kbOpen]);

  const loadConversations = useCallback(
    () =>
      api("/conversations")
        .then(setConversations)
        .catch(() => {}),
    [],
  );

  useEffect(() => {
    api("/kbs")
      .then((all) => setKbs(all.filter((k) => k.status === "ready")))
      .catch(setError);

    api("/app-info")
      .then(setInfo)
      .catch(() => {});

    loadConversations();
  }, [loadConversations]);

  const kb = params.get("kb") || kbs?.[0]?.kb_name || "";

  const current = kbs?.find((k) => k.kb_name === kb);

  useEffect(() => {
    if (!kb) return;

    setDetail(null);

    api(`/kbs/${kb}`).then(setDetail).catch(setError);
  }, [kb]);

  useEffect(() => {
    bottom.current?.scrollIntoView({
      behavior: "smooth",
    });
  }, [thread.length, pending]);

  useEffect(() => {
    if (!pending) return undefined;

    setQueryStep(0);

    const id = window.setInterval(
      () => setQueryStep((step) => Math.min(step + 1, 3)),
      900,
    );

    return () => window.clearInterval(id);
  }, [pending]);

  const pickKb = (name) => {
    if (name === kb) {
      setKbOpen(false);
      return;
    }

    generation.current += 1;

    setPending(false);
    setParams({ kb: name });
    setConvId(null);
    setThread([]);
    setError(null);
    setKbOpen(false);
  };

  const newChat = () => {
    generation.current += 1;

    setPending(false);
    setConvId(null);
    setThread([]);
    setError(null);
  };

  const openConversation = async (c) => {
    generation.current += 1;

    const mine = generation.current;

    setPending(false);

    try {
      const conv = await api(`/conversations/${c.id}`);

      if (mine !== generation.current) return;

      setParams({
        kb: conv.kb_name,
      });

      setConvId(conv.id);

      setThread(
        conv.messages.flatMap((m) => [
          {
            role: "me",
            text: m.question,
          },
          {
            role: "bot",
            ...m,
          },
        ]),
      );

      setError(null);
    } catch (err) {
      setError(err);
    }
  };

  const removeConversation = async (c) => {
    if (!window.confirm(`Delete the conversation "${c.title}"?`)) {
      return;
    }

    await api(`/conversations/${c.id}`, {
      method: "DELETE",
    }).catch(setError);

    if (c.id === convId) {
      newChat();
    }

    loadConversations();
  };

  const ask = async (e) => {
    e.preventDefault();

    const q = question.trim();

    if (!q || pending) return;

    setThread((t) => [
      ...t,
      {
        role: "me",
        text: q,
      },
    ]);

    setQuestion("");
    setPending(true);
    setQueryStep(0);
    setError(null);

    const mine = generation.current;

    try {
      const r = await api(`/kbs/${kb}/chat`, {
        method: "POST",
        json: {
          question: q,
          conversation_id: convId,
        },
      });

      loadConversations();

      if (mine !== generation.current) return;

      setConvId(r.conversation_id);

      setThread((t) => [
        ...t,
        {
          role: "bot",
          question: q,
          ...r,
        },
      ]);
    } catch (err) {
      if (mine === generation.current) {
        setError(err);
      }
    } finally {
      if (mine === generation.current) {
        setPending(false);
      }
    }
  };

  const stats = detail?.stats;

  return (
    <div
      className="split chat-layout"
      style={{
        display: "flex",
        flexGrow: 1,
        minHeight: 0,
        height: "calc(100vh - 56px)",
      }}
    >
      {/* =====================================================
          GPT-STYLE LEFT SIDEBAR
          ===================================================== */}

      <aside className="chat-sidebar gpt-sidebar">
        {/* Top section */}
        <div className="gpt-sidebar-top">
          {/* New Chat */}
          <button className="gpt-new-chat" onClick={newChat}>
            <span className="gpt-new-chat-icon" aria-hidden="true">
              ＋
            </span>

            <span>New chat</span>
          </button>

          {/* Knowledge Base */}
          <div
            ref={kbDropdown}
            className={`gpt-kb-wrapper${kbOpen ? " open" : ""}`}
          >
            <button
              type="button"
              className="gpt-kb-trigger"
              onClick={() => setKbOpen((open) => !open)}
              aria-expanded={kbOpen}
              aria-haspopup="listbox"
            >
              <span className="gpt-kb-left">
                <span className="gpt-kb-icon" aria-hidden="true">
                  ◈
                </span>

                <span className="gpt-kb-text">
                  <span className="gpt-kb-caption">Knowledge base</span>

                  <strong>
                    {current?.kb_name || "Select a knowledge base"}
                  </strong>
                </span>
              </span>

              <span className="gpt-chevron" aria-hidden="true">
                {kbOpen ? "⌃" : "⌄"}
              </span>
            </button>

            {kbOpen && (
              <div className="gpt-kb-panel">
                <div className="gpt-kb-panel-title">Knowledge bases</div>

                <div
                  className="gpt-kb-options"
                  role="listbox"
                  aria-label="Knowledge bases"
                >
                  {(kbs || []).map((k) => (
                    <button
                      key={k.kb_name}
                      className={`gpt-kb-option${
                        k.kb_name === kb ? " active" : ""
                      }`}
                      onClick={() => pickKb(k.kb_name)}
                      role="option"
                      aria-selected={k.kb_name === kb}
                    >
                      <div className="gpt-kb-option-main">
                        <span className="gpt-kb-option-name">{k.kb_name}</span>

                        <TypeBadge type={k.kb_type} />
                      </div>

                      <span className="gpt-kb-option-domain">
                        {k.domain} / {k.sub_domain}
                      </span>

                      <span className="gpt-kb-option-access">
                        {k.role === "owner" ? (
                          <span className="badge">Owner</span>
                        ) : (
                          <span className="badge g">User access</span>
                        )}
                      </span>
                    </button>
                  ))}

                  {kbs && kbs.length === 0 && (
                    <div className="muted small gpt-kb-empty">
                      No knowledge bases available.
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ===================================================
            RECENT CONVERSATIONS
            =================================================== */}

        <div className="gpt-history">
          <div className="gpt-history-title">Recent conversations</div>

          <div
            className="gpt-conversation-list"
            aria-label="Recent conversations"
          >
            {conversations.length === 0 && (
              <div className="gpt-empty-history">
                Your last {info?.conversations_kept || 10} conversations appear
                here.
              </div>
            )}

            {conversations.map((c) => (
              <div
                key={c.id}
                className={`gpt-conversation${
                  c.id === convId ? " active" : ""
                }`}
                role="button"
                tabIndex={0}
                onClick={() => openConversation(c)}
                onKeyDown={(e) => e.key === "Enter" && openConversation(c)}
              >
                <div className="gpt-conversation-content">
                  <div className="gpt-conversation-title">{c.title}</div>

                  <div className="gpt-conversation-meta">
                    {c.kb_name} · {formatDate(c.last_message_at, true)}
                  </div>
                </div>

                <button
                  className="gpt-delete"
                  aria-label={`Delete conversation ${c.title}`}
                  title="Delete conversation"
                  onClick={(e) => {
                    e.stopPropagation();
                    removeConversation(c);
                  }}
                >
                  <svg
                    width="15"
                    height="15"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    aria-hidden="true"
                  >
                    <path d="M3 6h18" />
                    <path d="M8 6V4h8v2" />
                    <path d="M19 6l-1 15H6L5 6" />
                    <path d="M10 11v6" />
                    <path d="M14 11v6" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* ===================================================
            MCP SERVER URL
            =================================================== */}

        <div className="gpt-sidebar-bottom">
          {info?.mcp_url && (
            <div className="gpt-mcp-box">
              <div className="gpt-mcp-head">
                <span>MCP server URL</span>

                <CopyButton text={info.mcp_url} />
              </div>

              <code title={info.mcp_url}>{info.mcp_url}</code>
            </div>
          )}
        </div>
      </aside>

      {/* =====================================================
          MAIN CHAT
          ===================================================== */}

      <main
        className="chat-main"
        style={{
          flexGrow: 1,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
        }}
      >
        {current ? (
          <>
            {/* Chat Header */}
            <div
              className="chat-header"
              style={{
                background: "#fff",
                borderBottom: "1px solid var(--line)",
                padding: "10px 32px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: 16,
              }}
            >
              <div
                style={{
                  minWidth: 0,
                }}
              >
                <div className="chat-title-row">
                  <span className="chat-title-dot" />

                  <div className="chat-title">{kb}</div>

                  <TypeBadge type={current.kb_type} />
                </div>

                {/* Graph Stats */}
                {stats && current.kb_type === "graph" && (
                  <div className="stats-overview">
                    <div className="stat-card">
                      <span className="stat-icon">◆</span>

                      <div>
                        <Breakdown
                          total={stats.entities}
                          kinds={stats.node_types}
                          noun="entities"
                          kindNoun="node types"
                          by={stats.by_label}
                        />
                      </div>
                    </div>

                    <div className="stat-card">
                      <span className="stat-icon relation">↗</span>

                      <div>
                        <Breakdown
                          total={stats.relationships}
                          kinds={stats.relationship_types}
                          noun="relationships"
                          kindNoun="relationship types"
                          by={stats.by_type}
                        />
                      </div>
                    </div>
                  </div>
                )}

                {/* RAG Stats */}
                {stats && current.kb_type === "rag" && (
                  <div className="stats-overview">
                    <div className="stat-card">
                      <span className="stat-icon">▤</span>

                      <div>
                        <strong>{stats.documents}</strong> documents
                      </div>
                    </div>

                    <div className="stat-card">
                      <span className="stat-icon relation">▦</span>

                      <div>
                        <strong>{stats.chunks}</strong> chunks
                      </div>
                    </div>
                  </div>
                )}
              </div>

              <button
                className="btn sec sm"
                style={{
                  flexShrink: 0,
                  whiteSpace: "nowrap",
                }}
                onClick={() => navigate(`/add-data?kb=${kb}`)}
              >
                Add data
              </button>
            </div>

            {/* Messages */}
            <div
              className="chat-messages"
              style={{
                flexGrow: 1,
                overflowY: "auto",
                padding: "24px 32px",
                display: "flex",
                flexDirection: "column",
                gap: 18,
              }}
            >
              {thread.length === 0 && !pending && (
                <div className="chat-empty">
                  <div className="chat-empty-icon">✦</div>

                  <strong>Explore your knowledge base</strong>

                  <span className="chat-empty-kb">{kb}</span>

                  <span className="muted">
                    {current.kb_type === "graph"
                      ? "I’ll find the answer in your graph and show the Cypher and path used."
                      : "I’ll search your documents and cite the passages used."}
                  </span>
                </div>
              )}

              {thread.map((m, i) =>
                m.role === "me" ? (
                  <div key={i} className="message-user">
                    <span className="message-user-label">You</span>

                    <div className="bubble me">{m.text}</div>
                  </div>
                ) : (
                  <Answer key={i} m={m} />
                ),
              )}

              {pending && (
                <QueryProgress type={current.kb_type} step={queryStep} />
              )}

              <ErrorBox error={error} />

              <div ref={bottom} />
            </div>

            {/* Composer */}
            <form
              className="chat-composer-wrap"
              onSubmit={ask}
              style={{
                padding: "0 32px 24px",
              }}
            >
              <div
                className="card chat-composer"
                style={{
                  display: "flex",
                  gap: 8,
                  padding: 8,
                  alignItems: "center",
                }}
              >
                <input
                  className="inp"
                  style={{
                    border: 0,
                  }}
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder={`Ask about ${kb}`}
                  aria-label="Question"
                  maxLength={2000}
                />

                <button
                  className="btn"
                  type="submit"
                  disabled={pending || !question.trim()}
                >
                  Send
                </button>
              </div>
            </form>
          </>
        ) : (
          <div className="page muted">
            {kbs && kbs.length === 0
              ? "You have no knowledge bases that are ready for chat."
              : "Loading…"}
          </div>
        )}
      </main>
    </div>
  );
}
