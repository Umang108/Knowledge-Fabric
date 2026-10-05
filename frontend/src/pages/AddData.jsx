import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, formatDate, formatSize } from "../api.js";
import { DropZone, ErrorBox, Modal, TypeBadge, usePoll } from "../components/common.jsx";
import { ConnectionsModal, DatasetPicker, datasetsPayload, initialSelection, presetsFor, useConnectors }
  from "../components/connectors.jsx";

const today = () => new Date().toISOString().slice(0, 10);

export default function AddData() {
  const [params, setParams] = useSearchParams();
  const [kbs, setKbs] = useState(null);
  const [detail, setDetail] = useState(null);
  const [runs, setRuns] = useState([]);
  const [files, setFiles] = useState([]);
  const [check, setCheck] = useState(null);
  const [merge, setMerge] = useState(true);
  const [skip, setSkip] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState(null);
  const navigate = useNavigate();
  const sources = useConnectors();
  const [pull, setPull] = useState(null); // {connId, selection, since, changesOnly}
  const [manage, setManage] = useState(false);

  useEffect(() => {
    api("/kbs").then((all) => setKbs(all.filter((k) => k.status === "ready"))).catch(setError);
  }, []);
  const kb = params.get("kb") || kbs?.[0]?.kb_name || "";
  const current = kbs?.find((k) => k.kb_name === kb);

  const loadRuns = useCallback(() => kb && api(`/kbs/${kb}/runs`).then(setRuns).catch(setError), [kb]);
  useEffect(() => {
    if (!kb) return;
    setDetail(null);
    api(`/kbs/${kb}`).then(setDetail).catch(setError);
    loadRuns();
  }, [kb, loadRuns]);
  const running = runs.some((r) => r.status === "running");
  usePoll(loadRuns, 2000, running);
  const wasRunning = useRef(false);
  useEffect(() => {  // refresh the stats line when a run finishes
    if (wasRunning.current && !running && kb) api(`/kbs/${kb}`).then(setDetail).catch(() => {});
    wasRunning.current = running;
  }, [running, kb]);

  const choose = async (list) => {
    setFiles(list);
    setCheck(null);
    setError(null);
    if (current?.kb_type !== "graph") return;
    const fd = new FormData();
    fd.append("file", list[0]);
    try {
      setCheck(await api(`/kbs/${kb}/add-data/check`, { method: "POST", form: fd }));
    } catch (e) {
      setCheck({ matched: false, error: e.message });
    }
  };

  const ingest = async () => {
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      fd.append("merge_existing", merge);
      fd.append("skip_invalid", skip);
      await api(`/kbs/${kb}/add-data`, { method: "POST", form: fd });
      setFiles([]);
      setCheck(null);
      setTimeout(loadRuns, 300);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const openReport = async (run) => setReport(await api(`/kbs/${kb}/runs/${run.id}/report`));

  const runBadge = (r) => {
    if (r.status === "running") return <span className="badge w">Running {Math.round(r.progress || 0)}%</span>;
    if (r.status === "failed") return <span className="badge r">Failed</span>;
    if (r.rows_rejected) return <span className="badge r">{r.rows_rejected} rows rejected</span>;
    if (r.run_type === "initial_build") return <span className="badge g">Initial build</span>;
    return <span className="badge">Completed</span>;
  };

  const isRag = current?.kb_type === "rag";
  // RAG stores take ServiceNow knowledge articles only; graphs take tables from any system
  const usable = (sources.connections || []).filter((c) =>
    Object.values(presetsFor(sources.kinds, c)).some((p) => !isRag || p.kb_type === "rag"));
  const pullConn = usable.find((c) => String(c.id) === String(pull?.connId));
  const startPull = (connId) => {
    const c = usable.find((x) => String(x.id) === String(connId));
    setPull({ connId: c?.id ?? "", selection: c ? initialSelection(sources.kinds, c, current?.kb_type) : null,
              since: "", changesOnly: false });
  };
  const pullNow = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api(`/kbs/${kb}/add-data/connection`, {
        method: "POST",
        json: { connection_id: pullConn.id, datasets: datasetsPayload(pull.selection),
                since: pull.changesOnly && pull.since ? pull.since : null,
                merge_existing: merge, skip_invalid: skip },
      });
      navigate(`/kbs/${kb}/jobs/${r.job_id}`);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const stats = detail?.stats;
  const isGraph = current?.kb_type === "graph";
  const matchedSheets = check?.sheets || [];

  return (
    <div className="page">
      <div>
        <h1>Add data to an existing knowledge base</h1>
        <div className="sub">
          {isGraph || !current ? "The schema already exists, so new rows are mapped automatically. There is no review step."
                               : "New documents are chunked and added to the same vector store. There is no review step."}
        </div>
      </div>
      <div className="split" style={{ display: "flex", gap: 24, alignItems: "flex-start" }}>
        <div className="card" style={{ flex: 1.4, padding: 22, display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          <div>
            <label className="lbl" htmlFor="kb">Knowledge base</label>
            <select id="kb" className="inp" value={kb} onChange={(e) => { setParams({ kb: e.target.value }); setFiles([]); setCheck(null); setPull(null); }}>
              {(kbs || []).map((k) => (
                <option key={k.kb_name} value={k.kb_name}>{k.kb_name} ({k.kb_type === "graph" ? "Graph" : "RAG"}, {k.role})</option>
              ))}
            </select>
          </div>
          {kbs && kbs.length === 0 && <div className="notice">No ready knowledge bases to add data to.</div>}
          {current && (
            <div style={{ background: "var(--bg)", borderRadius: 10, padding: "12px 14px", display: "flex", gap: 12,
                          alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
              <TypeBadge type={current.kb_type} />
              {stats && isGraph && <span>{stats.node_types} node types, {stats.relationship_types} relationship types, {stats.entities.toLocaleString()} entities</span>}
              {stats && !isGraph && <span>{stats.documents} documents, {stats.chunks} chunks</span>}
              <span className="muted">Last updated {formatDate(runs[0]?.finished_at || detail?.updated_at)}</span>
            </div>
          )}
          {current && (
            <DropZone accept={isGraph ? ".csv,.xlsx,.xlsm" : ".pdf,.docx,.txt,.md"} multiple={!isGraph}
                      title={isGraph ? "Drop new CSV or XLSX here" : "Drop PDF, DOCX or TXT here"}
                      hint={isGraph ? ". For RAG bases, drop PDF, DOCX or TXT instead." : ""} onFiles={choose} />
          )}
          {files.map((f) => (
            <div key={f.name} className="file" style={{ background: "var(--bg)", borderRadius: 10, padding: "10px 14px" }}>
              <div style={{ flexGrow: 1 }}>
                <div style={{ fontWeight: 600 }}>{f.name}</div>
                <div className="muted small">
                  {formatSize(f.size)}
                  {check?.matched && `, ${matchedSheets.length} sheet${matchedSheets.length > 1 ? "s" : ""}, ${check.total_rows.toLocaleString()} rows. `
                    + matchedSheets.map((s) => `${s.file_sheet} matches ${s.schema_sheet}`).join("; ") + "."}
                  {check?.unmatched_sheets?.length > 0 && ` Skipped: ${check.unmatched_sheets.join(", ")}.`}
                  {check?.error && ` ${check.error}`}
                </div>
              </div>
              {isGraph && check && (check.matched ? <span className="badge">Schema matched</span> : <span className="badge r">No match</span>)}
            </div>
          ))}
          {current && (
            <div style={{ borderTop: "1px solid var(--line-soft)", paddingTop: 14, display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div style={{ fontWeight: 600 }}>Or pull from a connected system</div>
                <button className="btn link" type="button" onClick={() => setManage(true)}>Manage connections</button>
              </div>
              {usable.length === 0 ? (
                <div className="muted small">
                  {isRag ? "Add a ServiceNow connection to pull knowledge articles into this store."
                         : "Add a SAP or ServiceNow connection to pull new rows into this graph."}
                </div>
              ) : (
                <select className="inp" aria-label="Connection to pull from" value={pull?.connId ?? ""}
                        onChange={(e) => (e.target.value ? startPull(e.target.value) : setPull(null))}>
                  <option value="">Choose a connection…</option>
                  {usable.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.label})</option>)}
                </select>
              )}
              {pullConn && pull.selection && (
                <>
                  <DatasetPicker kinds={sources.kinds} connection={pullConn} kbType={current.kb_type}
                                 value={pull.selection} onChange={(selection) => setPull({ ...pull, selection })} />
                  <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                    <label style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      <input type="checkbox" checked={pull.changesOnly}
                             onChange={(e) => setPull({ ...pull, changesOnly: e.target.checked, since: pull.since || today() })} />
                      Only records changed since
                    </label>
                    <input type="date" className="inp sm" style={{ width: 170 }} aria-label="Changed since date"
                           disabled={!pull.changesOnly} value={pull.since} max={today()}
                           onChange={(e) => setPull({ ...pull, since: e.target.value })} />
                  </div>
                  {pullConn.kind === "sap" && pull.changesOnly && (
                    <div className="muted small">SAP tables need their changed field set (e.g. LastChangeDateTime).</div>
                  )}
                </>
              )}
            </div>
          )}
          {isGraph && (
            <>
              <label style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <input type="checkbox" checked={merge} onChange={(e) => setMerge(e.target.checked)} />
                Merge with existing nodes using their key property, so duplicates are not created.
              </label>
              <label style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <input type="checkbox" checked={skip} onChange={(e) => setSkip(e.target.checked)} />
                Skip rows that do not fit the schema and list them in the run report.
              </label>
            </>
          )}
          <ErrorBox error={error} />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 12 }}>
            <button className="btn sec" onClick={() => { setFiles([]); setCheck(null); }}>Clear</button>
            {pullConn && pull?.selection && (
              <button className="btn sec" onClick={pullNow}
                      disabled={busy || !datasetsPayload(pull.selection).length || (pull.changesOnly && !pull.since)}>
                {busy ? "Starting…" : `Pull from ${pullConn.label}`}
              </button>
            )}
            <button className="btn" onClick={ingest} disabled={busy || !files.length || (isGraph && !check?.matched)}>
              {busy ? "Starting…" : "Ingest data"}
            </button>
          </div>
        </div>

        <div className="card" style={{ flex: 1, padding: 22, minWidth: 0 }}>
          <div className="section" style={{ marginBottom: 8 }}>Pipeline runs</div>
          {runs.length === 0 && <div className="muted small">No runs yet.</div>}
          {runs.map((r) => (
            <div key={r.id} style={{ padding: "14px 0", borderBottom: "1px solid var(--line-soft)" }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
                <div style={{ fontWeight: 600 }}>Run {r.run_no}, {r.source_file}</div>
                {runBadge(r)}
              </div>
              <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                {r.summary || "Mapping rows to the existing schema and writing to the graph."}{" "}
                {r.finished_at && formatDate(r.finished_at, true)}{" "}
                {r.rows_rejected > 0 && <button className="btn link" style={{ fontSize: 13 }} onClick={() => openReport(r)}>View report</button>}
              </div>
              {r.status === "running" && <div className="bar" style={{ marginTop: 8 }}><div style={{ width: `${r.progress || 0}%` }} /></div>}
            </div>
          ))}
        </div>
      </div>
      {manage && (
        <ConnectionsModal kinds={sources.kinds} connections={sources.connections} reload={sources.reload}
                          onClose={() => setManage(false)} />
      )}
      {report && (
        <Modal title={`Run ${report.run_no}: rejected rows`} onClose={() => setReport(null)}>
          <div className="muted small" style={{ marginBottom: 12 }}>{report.source_file}. {report.summary}</div>
          <table>
            <thead><tr><th>Sheet</th><th>Row</th><th>Reason</th></tr></thead>
            <tbody>
              {report.rejected_report.map((x, i) => (
                <tr key={i}><td>{x.sheet}</td><td>{x.row}</td><td>{x.reason}</td></tr>
              ))}
            </tbody>
          </table>
        </Modal>
      )}
    </div>
  );
}
