import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import { ErrorBox, usePoll } from "../components/common.jsx";

const TITLES = {
  graph_extract: "Extracting your knowledge graph", graph_build: "Building your knowledge graph",
  rag_ingest: "Indexing your documents", add_data: "Adding data",
};

export default function Processing() {
  const { kb, jobId } = useParams();
  const navigate = useNavigate();
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(() => api(`/jobs/${jobId}`).then(setJob).catch(setError), [jobId]);
  useEffect(() => { load(); }, [load]);
  const running = !job || ["queued", "running"].includes(job.status);
  usePoll(load, 1500, running);

  useEffect(() => {
    if (job?.status !== "succeeded") return;
    const t = setTimeout(() => {
      if (job.job_type === "graph_extract") navigate(`/kbs/${kb}/review`, { replace: true });
      else if (job.job_type === "graph_build") navigate(`/chat?kb=${kb}`, { replace: true });
    }, 800);
    return () => clearTimeout(t);
  }, [job, kb, navigate]);

  const cancel = async () => {
    try {
      await api(`/jobs/${jobId}/cancel`, { method: "POST" });
      load();
    } catch (e) {
      setError(e);
    }
  };

  return (
    <div className="page" style={{ alignItems: "center", justifyContent: "center", background: "#e9ebea" }}>
      <div className="card" style={{ width: 600, maxWidth: "100%", padding: 32, display: "flex", flexDirection: "column", gap: 20 }}>
        <div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>{TITLES[job?.job_type] || "Working"}</div>
          <div className="muted">{job?.source_file} for {kb}</div>
        </div>
        <div className="bar" aria-label="progress" aria-valuenow={Math.round(job?.progress || 0)} role="progressbar">
          <div style={{ width: `${job?.progress || 0}%` }} />
        </div>
        <div>
          {(job?.steps || []).map((s, i) => {
            const state = job.status === "failed" && s.status === "running" ? "failed" : s.status;
            return (
              <div className="step" key={s.name}>
                <div className={`num ${state}`}>{i + 1}</div>
                <div style={{ flexGrow: 1, fontWeight: state === "running" ? 600 : 400,
                              color: state === "waiting" ? "var(--muted)" : undefined }}>{s.name}</div>
                <div className="small" style={{ color: state === "running" ? "var(--amber)" : "var(--muted)",
                                                  fontWeight: state === "running" ? 600 : 400 }}>
                  {state === "running" ? (s.detail || "In progress") : state === "waiting" ? "Waiting" : s.detail}
                </div>
              </div>
            );
          })}
        </div>
        {job?.status === "failed" && <div className="error" role="alert">{job.error}</div>}
        {job?.status === "cancelled" && <div className="error">This job was cancelled.</div>}
        <ErrorBox error={error} />
        {running ? (
          <div className="notice" style={{ background: "var(--bg)", color: "#46524f", fontSize: 13 }}>
            You can leave this page. {job?.job_type === "graph_extract"
              ? "The knowledge base will show “Awaiting review” in your workspace when extraction finishes."
              : "Progress is also shown in your workspace."}
          </div>
        ) : null}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          {running ? <button className="btn sec" onClick={cancel}>Cancel</button>
                   : <Link className="btn sec" to="/workspace">Back to workspace</Link>}
          {job?.job_type === "graph_extract" && (
            <button className="btn link" disabled={job.status !== "succeeded"}
                    onClick={() => navigate(`/kbs/${kb}/review`)}>Skip ahead to review</button>
          )}
        </div>
      </div>
    </div>
  );
}
