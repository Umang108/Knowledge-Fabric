import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatSize } from "../api.js";
import {
  DropZone,
  ErrorBox,
  RoleBadge,
  StatusText,
  TypeBadge,
  usePoll,
} from "../components/common.jsx";
import {
  ConnectionsModal,
  DatasetPicker,
  datasetsPayload,
  initialSelection,
  useConnectors,
} from "../components/connectors.jsx";

const GRAPH_ACCEPT = ".csv,.xlsx,.xlsm";
const RAG_ACCEPT = ".pdf,.docx,.txt,.md";
const BUSY = ["extracting", "building", "ingesting"];

const suggestName = (file, kind) =>
  file.name
    .replace(/\.[^.]+$/, "")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^(\d)/, "kb_$1")
    .slice(0, 55) + (kind === "graph" ? "_kg" : "_rag");

export default function Workspace() {
  const navigate = useNavigate();
  const [kbs, setKbs] = useState(null);
  const [mode, setMode] = useState("graph");
  const [graphFile, setGraphFile] = useState(null);
  const [ragFiles, setRagFiles] = useState([]);
  const [ragRuns, setRagRuns] = useState({ kb: null, runs: [] });
  const [form, setForm] = useState({ name: "", domain: "", sub: "" });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(null);
  const sources = useConnectors();
  const [manage, setManage] = useState(false);
  const [connId, setConnId] = useState("");
  const [selection, setSelection] = useState(null);
  const connection = (sources.connections || []).find(
    (c) => String(c.id) === String(connId),
  );
  const firstConn = sources.connections?.[0]?.id;
  useEffect(() => {
    if (!connId && firstConn) setConnId(String(firstConn));
  }, [connId, firstConn]);

  const load = useCallback(() => api("/kbs").then(setKbs).catch(setError), []);
  useEffect(() => {
    load();
  }, [load]);
  usePoll(load, 3000, !!kbs && kbs.some((k) => BUSY.includes(k.status)));
  usePoll(
    () =>
      ragRuns.kb &&
      api(`/kbs/${ragRuns.kb}/runs`).then((runs) =>
        setRagRuns((r) => ({ ...r, runs })),
      ),
    2000,
    !!ragRuns.kb &&
      (ragRuns.runs.length === 0 ||
        ragRuns.runs.some((r) => r.status === "running")),
  );

  const fromSource = mode === "source";
  const files = fromSource
    ? []
    : mode === "graph"
      ? graphFile
        ? [graphFile]
        : []
      : ragFiles;
  const kbType = fromSource ? selection?.kbType || "graph" : mode;
  const useSource = () => {
    const sel = initialSelection(sources.kinds, connection);
    setMode("source");
    setError(null);
    setSelection(sel);
    setForm((f) => ({
      ...f,
      name:
        f.name ||
        `${connection.kind}_${sel.preset}`
          .toLowerCase()
          .replace(/[^a-z0-9_]/g, "_") +
          (sel.kbType === "graph" ? "_kg" : "_rag"),
    }));
  };
  const choose = (kind, list) => {
    setMode(kind);
    setError(null);
    if (kind === "graph") setGraphFile(list[0]);
    else setRagFiles(list);
    setForm((f) => ({ ...f, name: f.name || suggestName(list[0], kind) }));
  };
  const reset = () => {
    setGraphFile(null);
    setRagFiles([]);
    setForm({ name: "", domain: "", sub: "" });
    setError(null);
    if (fromSource) {
      setMode("graph");
      setSelection(null);
    }
  };

  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (fromSource) {
        const r = await api("/kbs/from-connection", {
          method: "POST",
          json: {
            kb_name: form.name.trim(),
            kb_type: kbType,
            domain: form.domain.trim(),
            sub_domain: form.sub.trim(),
            connection_id: connection.id,
            datasets: datasetsPayload(selection),
          },
        });
        navigate(`/kbs/${r.kb_name}/jobs/${r.job_id}`);
        return;
      }
      const fd = new FormData();
      fd.append("kb_name", form.name.trim());
      fd.append("kb_type", mode);
      fd.append("domain", form.domain.trim());
      fd.append("sub_domain", form.sub.trim());
      files.forEach((f) => fd.append("files", f));
      const r = await api("/kbs", { method: "POST", form: fd });
      if (mode === "graph") {
        navigate(`/kbs/${r.kb_name}/jobs/${r.job_id}`);
      } else {
        navigate(`/kbs/${r.kb_name}/jobs/${r.job_id}`);
      }
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (kb) => {
    if (
      !window.confirm(
        `Delete knowledge base "${kb.kb_name}"? This cannot be undone.`,
      )
    )
      return;
    setDeleting(kb.kb_name);
    setError(null);
    try {
      await api(`/kbs/${kb.kb_name}`, { method: "DELETE" });
      if (ragRuns.kb === kb.kb_name) setRagRuns({ kb: null, runs: [] });
      load();
    } catch (err) {
      setError(err);
    } finally {
      setDeleting(null);
    }
  };

  const action = (kb) => {
    let primary = null;
    if (kb.status === "awaiting_review" && kb.role === "owner")
      primary = (
        <button
          className="act"
          onClick={() => navigate(`/kbs/${kb.kb_name}/review`)}
        >
          Review
        </button>
      );
    else if (BUSY.includes(kb.status) && kb.last_job_id)
      primary = (
        <button
          className="act"
          onClick={() => navigate(`/kbs/${kb.kb_name}/jobs/${kb.last_job_id}`)}
        >
          View progress
        </button>
      );
    else if (
      kb.status === "failed" &&
      kb.role === "owner" &&
      kb.kb_type === "graph"
    )
      primary = (
        <button
          className="act"
          onClick={async () => {
            try {
              const r = await api(`/kbs/${kb.kb_name}/extract`, {
                method: "POST",
              });
              navigate(`/kbs/${kb.kb_name}/jobs/${r.job_id}`);
            } catch (err) {
              setError(err);
            }
          }}
        >
          Retry extraction
        </button>
      );
    else if (kb.status === "ready" && kb.role === "owner")
      primary = (
        <button
          className="act"
          onClick={() => navigate(`/access?kb=${kb.kb_name}`)}
        >
          Manage access
        </button>
      );
    else if (kb.status === "ready")
      primary = (
        <button
          className="act"
          onClick={() => navigate(`/chat?kb=${kb.kb_name}`)}
        >
          Chat
        </button>
      );
    if (kb.role !== "owner") return primary;
    return (
      <div className="acts" style={{ justifyContent: "flex-end" }}>
        {primary}
        <button
          className="act danger"
          onClick={() => remove(kb)}
          disabled={deleting === kb.kb_name}
        >
          {deleting === kb.kb_name ? "Deleting…" : "Delete"}
        </button>
      </div>
    );
  };

  const runBadge = (r) =>
    r.status === "completed" ? (
      <span className="badge">Indexed</span>
    ) : r.status === "failed" ? (
      <span className="badge r" title={r.summary}>
        Failed
      </span>
    ) : (
      <span className="badge w">Embedding {Math.round(r.progress || 0)}%</span>
    );

  return (
    <div
      className="split workspace-page"
      style={{ display: "flex", flexGrow: 1, minHeight: 0 }}
    >
      <aside
        className="workspace-sidebar"
        style={{
          width: 340,
          background: "#fff",
          borderRight: "1px solid var(--line)",
          padding: 16,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <div className="section">Upload data</div>
        <div
          className={`card workspace-upload-card workspace-source-card${mode === "rag" ? " active" : ""}`}
          style={{
            padding: 18,
            display: "flex",
            flexDirection: "column",
            gap: 12,
            border: mode === "rag" ? "2px solid var(--teal)" : undefined,
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 600 }}>RAG documents</div>
            <span className="badge g">Vector store</span>
          </div>
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.45 }}>
            PDF, DOCX or TXT. Chunked and embedded automatically in the
            background, no review step.
          </div>
          <DropZone
            accept={RAG_ACCEPT}
            multiple
            title="Drop files here"
            onFiles={(l) => choose("rag", l)}
          />
          <div>
            {ragFiles.map((f) => (
              <div className="file" key={f.name}>
                <span style={{ flexGrow: 1 }}>{f.name}</span>
                <span className="muted small">{formatSize(f.size)}</span>
              </div>
            ))}
            {ragRuns.runs.map((r) => (
              <div className="file" key={r.id}>
                <span style={{ flexGrow: 1 }}>{r.source_file}</span>
                {runBadge(r)}
              </div>
            ))}
          </div>
        </div>
        <div
          className={`card workspace-upload-card workspace-source-card${mode === "graph" ? " active" : ""}`}
          style={{
            padding: 18,
            display: "flex",
            flexDirection: "column",
            gap: 12,
            border: mode === "graph" ? "2px solid var(--teal)" : undefined,
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 600 }}>
              Knowledge graph data
            </div>
            <span className="badge b">Graph</span>
          </div>
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.45 }}>
            CSV or XLSX. An LLM extracts nodes, relationships and Cypher. You
            review them before the graph is built.
          </div>
          <DropZone
            accept={GRAPH_ACCEPT}
            title="Drop CSV or XLSX here"
            onFiles={(l) => choose("graph", l)}
          />
          {graphFile && (
            <div
              className="file"
              style={{
                background: "var(--bg)",
                borderRadius: 10,
                padding: "10px 12px",
              }}
            >
              <div style={{ flexGrow: 1 }}>
                <div style={{ fontWeight: 600 }}>{graphFile.name}</div>
                <div className="muted small">{formatSize(graphFile.size)}</div>
              </div>
              <button
                className="btn sec sm"
                type="button"
                onClick={() => setGraphFile(null)}
                aria-label={`Remove ${graphFile.name}`}
              >
                Remove
              </button>
            </div>
          )}
        </div>
        <div
          className={`card workspace-upload-card workspace-source-card${fromSource ? " active" : ""}`}
          style={{
            padding: 18,
            display: "flex",
            flexDirection: "column",
            gap: 12,
            border: fromSource ? "2px solid var(--teal)" : undefined,
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 600 }}>
              Connected systems
            </div>
            <span className="badge w">SAP · ServiceNow</span>
          </div>
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.45 }}>
            Pull tables straight from SAP (OData) or ServiceNow. They go through
            the same extraction and review as an uploaded workbook.
          </div>
          {sources.connections?.length ? (
            <div style={{ display: "flex", gap: 8 }}>
              <select
                className="inp"
                aria-label="Connection"
                value={connId}
                onChange={(e) => {
                  setConnId(e.target.value);
                  if (fromSource) setMode("graph");
                }}
              >
                {sources.connections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} ({c.label})
                  </option>
                ))}
              </select>
              <button
                className="btn sec"
                type="button"
                disabled={!connection}
                onClick={useSource}
              >
                Use
              </button>
            </div>
          ) : (
            <div className="muted small">No connections yet.</div>
          )}
          <ErrorBox error={sources.error} />
          <button
            className="btn link"
            type="button"
            style={{ alignSelf: "flex-start" }}
            onClick={() => setManage(true)}
          >
            Manage connections
          </button>
        </div>
      </aside>
      {manage && (
        <ConnectionsModal
          kinds={sources.kinds}
          connections={sources.connections}
          reload={sources.reload}
          onClose={() => setManage(false)}
        />
      )}

      <main className="page workspace-main" style={{ minWidth: 0 }}>
        <div className="workspace-heading">
          <div className="workspace-kicker">YOUR DATA WORKSPACE</div>
          <h1>Create knowledge base</h1>
          <div className="sub">
            You become the owner and are the only person who can grant access.
          </div>
        </div>
        <form
          className="card workspace-create-form"
          onSubmit={create}
          style={{
            padding: 24,
            display: "flex",
            flexDirection: "column",
            gap: 16,
          }}
        >
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            {kbType === "graph" ? (
              <span className="badge b">Knowledge graph</span>
            ) : (
              <span className="badge g">RAG store</span>
            )}
            <span className="muted small">
              {fromSource
                ? `Source: ${connection?.name} (${connection?.label})`
                : files.length
                  ? `Source: ${files.map((f) => f.name).join(", ")}`
                  : "Drop a file in the upload panel or pick a connected system to start"}
            </span>
          </div>
          {fromSource && selection && connection && (
            <DatasetPicker
              kinds={sources.kinds}
              connection={connection}
              value={selection}
              onChange={setSelection}
            />
          )}
          <div>
            <label className="lbl req" htmlFor="kbname">
              {kbType === "graph"
                ? "Knowledge graph name"
                : "Knowledge base name"}
            </label>
            <input
              id="kbname"
              className="inp"
              value={form.name}
              placeholder="e.g. Retail_Supply_Chain_KG"
              pattern="[A-Za-z][A-Za-z0-9_]{2,62}"
              title="3-63 characters: letters (upper or lower case), digits and underscores, starting with a letter"
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </div>
          <div className="row">
            <div className="grow">
              <label className="lbl req" htmlFor="domain">
                Domain
              </label>
              <input
                id="domain"
                className="inp"
                value={form.domain}
                placeholder="e.g. Retail"
                onChange={(e) => setForm({ ...form, domain: e.target.value })}
                required
              />
            </div>
            <div className="grow">
              <label className="lbl req" htmlFor="sub">
                Sub-domain
              </label>
              <input
                id="sub"
                className="inp"
                value={form.sub}
                placeholder="e.g. Supply chain"
                onChange={(e) => setForm({ ...form, sub: e.target.value })}
                required
              />
            </div>
          </div>
          <ErrorBox error={error} />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 12 }}>
            <button className="btn sec" type="button" onClick={reset}>
              Cancel
            </button>
            <button
              className="btn"
              type="submit"
              disabled={
                busy ||
                (fromSource
                  ? !selection || !datasetsPayload(selection).length
                  : !files.length)
              }
            >
              {busy
                ? fromSource
                  ? "Starting…"
                  : "Uploading…"
                : kbType === "graph"
                  ? fromSource
                    ? "Pull and extract graph"
                    : "Create and extract graph"
                  : fromSource
                    ? "Pull and index articles"
                    : "Create and index documents"}
            </button>
          </div>
        </form>

        <div className="workspace-list-heading">
          <div className="section">Your knowledge bases</div>
          <span className="muted small">{kbs?.length || 0} total</span>
        </div>
        <div className="card workspace-kb-table" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Domain / sub-domain</th>
                <th>Your role</th>
                <th>Status</th>
                <th style={{ width: 210 }} />
              </tr>
            </thead>
            <tbody>
              {kbs && kbs.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted">
                    No knowledge bases yet. Upload a file to create one.
                  </td>
                </tr>
              )}
              {(kbs || []).map((kb) => (
                <tr key={kb.kb_name}>
                  <td style={{ fontWeight: 600 }}>{kb.kb_name}</td>
                  <td>
                    <TypeBadge type={kb.kb_type} />
                  </td>
                  <td>
                    {kb.domain} / {kb.sub_domain}
                  </td>
                  <td>
                    <RoleBadge role={kb.role} status={kb.status} />
                  </td>
                  <td>
                    <StatusText kb={kb} />
                  </td>
                  <td style={{ textAlign: "right" }}>{action(kb)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
