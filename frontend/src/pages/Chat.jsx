import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api.js";
import { ErrorBox, TypeBadge } from "../components/common.jsx";

function Answer({ m }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, alignSelf: "flex-start", maxWidth: 760, width: "100%" }}>
      <div className="bubble bot">{m.answer}</div>
      {m.path?.length > 0 && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span className="muted small">Graph path</span>
          {m.path.map((p, i) => p.kind === "rel"
            ? <span key={i} className="rel">{p.text}</span>
            : <span key={i} className="chip">{p.text}</span>)}
        </div>
      )}
      {m.cypher && (
        <div className="code">
          <div className="head"><span>CYPHER USED</span>{m.row_count != null && <span>{m.row_count} rows</span>}</div>
          <pre>{m.cypher}</pre>
        </div>
      )}
      {m.sources?.length > 0 && (
        <div className="card" style={{ padding: "10px 14px" }}>
          <div className="section" style={{ marginBottom: 6 }}>Sources</div>
          {m.sources.slice(0, 4).map((s) => (
            <div key={s.n} className="small" style={{ padding: "4px 0" }}>
              <strong>[{s.n}] {s.source}{s.page ? `, page ${s.page}` : ""}</strong>
              <span className="muted">: {s.snippet.slice(0, 160)}…</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Chat() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [kbs, setKbs] = useState(null);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState(null);
  const [threads, setThreads] = useState({});   // kb -> [{role, ...}]
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const bottom = useRef(null);

  useEffect(() => {
    api("/kbs").then((all) => setKbs(all.filter((k) => k.status === "ready"))).catch(setError);
  }, []);
  const kb = params.get("kb") || kbs?.[0]?.kb_name || "";
  const current = kbs?.find((k) => k.kb_name === kb);
  const thread = threads[kb] || [];

  useEffect(() => {
    if (!kb) return;
    setDetail(null);
    api(`/kbs/${kb}`).then(setDetail).catch(setError);
  }, [kb]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [thread.length, pending]);

  const ask = async (e) => {
    e.preventDefault();
    const q = question.trim();
    if (!q || pending) return;
    const history = thread.filter((m) => m.role === "bot").slice(-3).map((m) => ({ question: m.question, answer: m.answer, cypher: m.cypher || "" }));
    setThreads((t) => ({ ...t, [kb]: [...(t[kb] || []), { role: "me", text: q }] }));
    setQuestion("");
    setPending(true);
    setError(null);
    try {
      const r = await api(`/kbs/${kb}/chat`, { method: "POST", json: { question: q, history } });
      setThreads((t) => ({ ...t, [kb]: [...(t[kb] || []), { role: "bot", question: q, ...r }] }));
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  };

  const shown = (kbs || []).filter((k) => `${k.kb_name} ${k.domain} ${k.sub_domain}`.toLowerCase().includes(search.toLowerCase()));
  const stats = detail?.stats;

  return (
    <div className="split" style={{ display: "flex", flexGrow: 1, minHeight: 0, height: "calc(100vh - 56px)" }}>
      <aside style={{ width: 340, background: "#fff", borderRight: "1px solid var(--line)", padding: 20, display: "flex",
                      flexDirection: "column", gap: 12 }}>
        <div className="section">Chat with</div>
        <input className="inp" placeholder="Search knowledge bases" value={search} onChange={(e) => setSearch(e.target.value)}
               aria-label="Search knowledge bases" />
        <div style={{ display: "flex", flexDirection: "column", gap: 6, overflowY: "auto", flexGrow: 1 }}>
          {shown.map((k) => (
            <button key={k.kb_name} className={`kbcard${k.kb_name === kb ? " on" : ""}`} onClick={() => setParams({ kb: k.kb_name })}>
              <div style={{ display: "flex", justifyContent: "space-between", width: "100%", alignItems: "center" }}>
                <span style={{ fontWeight: 600, fontSize: 15 }}>{k.kb_name}</span><TypeBadge type={k.kb_type} />
              </div>
              <span className="muted">{k.domain} / {k.sub_domain}</span>
              <span>{k.role === "owner" ? <span className="badge">Owner</span> : <span className="badge g">User access</span>}</span>
            </button>
          ))}
          {kbs && shown.length === 0 && <div className="muted small">No knowledge bases match.</div>}
        </div>
        <div className="muted small">You only see knowledge bases you own or were given access to.</div>
      </aside>

      <main style={{ flexGrow: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {current ? (
          <>
            <div style={{ background: "#fff", borderBottom: "1px solid var(--line)", padding: "14px 32px", display: "flex",
                          justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ fontWeight: 700, fontSize: 17 }}>{kb}</div>
                <div className="muted small">
                  Answers use only this {current.kb_type === "graph" ? "graph" : "document store"}.
                  {stats && current.kb_type === "graph" && ` ${stats.entities.toLocaleString()} entities, ${stats.relationships.toLocaleString()} relationships.`}
                  {stats && current.kb_type === "rag" && ` ${stats.documents} documents, ${stats.chunks} chunks.`}
                </div>
              </div>
              <button className="btn sec sm" onClick={() => navigate(`/add-data?kb=${kb}`)}>Add data</button>
            </div>
            <div style={{ flexGrow: 1, overflowY: "auto", padding: "24px 32px", display: "flex", flexDirection: "column", gap: 18 }}>
              {thread.length === 0 && !pending && (
                <div className="muted" style={{ margin: "auto", textAlign: "center", maxWidth: 420 }}>
                  Ask a question about {kb}. {current.kb_type === "graph"
                    ? "The answer shows the Cypher query and graph path it used."
                    : "The answer cites the document passages it used."}
                </div>
              )}
              {thread.map((m, i) => m.role === "me"
                ? <div key={i} className="bubble me">{m.text}</div>
                : <Answer key={i} m={m} />)}
              {pending && <div className="bubble bot muted">{current.kb_type === "graph" ? "Querying the graph…" : "Searching the documents…"}</div>}
              <ErrorBox error={error} />
              <div ref={bottom} />
            </div>
            <form onSubmit={ask} style={{ padding: "0 32px 24px" }}>
              <div className="card" style={{ display: "flex", gap: 8, padding: 8, alignItems: "center" }}>
                <input className="inp" style={{ border: 0 }} value={question} onChange={(e) => setQuestion(e.target.value)}
                       placeholder={`Ask about ${kb}`} aria-label="Question" />
                <button className="btn" type="submit" disabled={pending || !question.trim()}>Send</button>
              </div>
            </form>
          </>
        ) : (
          <div className="page muted">{kbs && kbs.length === 0 ? "You have no knowledge bases that are ready for chat." : "Loading…"}</div>
        )}
      </main>
    </div>
  );
}
