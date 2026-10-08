import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, formatDate } from "../api.js";
import { ErrorBox } from "../components/common.jsx";

const DOT = { granted: "var(--teal)", revoked: "var(--red)", created: "var(--blue)" };

export default function Access() {
  const [params, setParams] = useSearchParams();
  const [owned, setOwned] = useState(null);
  const [data, setData] = useState(null);
  const [userId, setUserId] = useState("");
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const kb = params.get("kb") || owned?.[0]?.kb_name || "";

  useEffect(() => {
    api("/kbs").then((all) => setOwned(all.filter((k) => k.role === "owner"))).catch(setError);
  }, []);

  const load = useCallback(() => {
    if (!kb) return;
    setData(null);
    api(`/kbs/${kb}/access`).then(setData).catch(setError);
  }, [kb]);
  useEffect(() => { load(); }, [load]);

  const grant = async (e) => {
    e.preventDefault();
    setError(null);
    setMessage(null);
    try {
      const r = await api(`/kbs/${kb}/access`, { method: "POST", json: { user_id: userId.trim(), role: "user" } });
      setMessage(`${r.display_name} (${r.user_id}) can now chat with and add data to ${kb}.`);
      setUserId("");
      load();
    } catch (err) {
      setError(err);
    }
  };

  const revoke = async (uid) => {
    if (!window.confirm(`Revoke ${uid}'s access to ${kb}?`)) return;
    setError(null);
    try {
      await api(`/kbs/${kb}/access/${encodeURIComponent(uid)}`, { method: "DELETE" });
      setMessage(`${uid}'s access was revoked.`);
      load();
    } catch (err) {
      setError(err);
    }
  };

  const current = owned?.find((k) => k.kb_name === kb);

  return (
    <div className="page split" style={{ flexDirection: "row", alignItems: "flex-start", gap: 24 }}>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
        <div>
          <h1>Manage access</h1>
          <div className="sub">Choose one of the knowledge bases you own to control who can use it.</div>
        </div>
        {owned && owned.length === 0 && (
          <div className="notice">You don&apos;t own any knowledge bases yet. Only the owner of a knowledge base can manage its access.</div>
        )}
        {current && (
          <div className="notice">
            <strong style={{ color: "var(--teal-dark)" }}>You own this knowledge base.</strong> Only the owner can grant or
            revoke access. People you add can chat with it and add data, but cannot share it.
          </div>
        )}
        <ErrorBox error={error} />
        {message && <div className="notice" role="status">{message}</div>}
        {current && (
          <form className="card" onSubmit={grant} style={{ padding: 20, display: "flex", flexDirection: "column", gap: 12 }}>
            <div className="section">Grant access</div>
            <div className="row" style={{ flexWrap: "wrap" }}>
              <div className="grow" style={{ minWidth: 200 }}>
                <label className="lbl req" htmlFor="grant-user">User ID</label>
                <input id="grant-user" className="inp" value={userId} onChange={(e) => setUserId(e.target.value)}
                       placeholder="e.g. meera.s" required />
              </div>
              <div style={{ width: 220 }}>
                <label className="lbl req" htmlFor="grant-role">Role</label>
                <select id="grant-role" className="inp" defaultValue="user">
                  <option value="user">User (chat and add data)</option>
                </select>
              </div>
              <button className="btn" type="submit">Grant access</button>
            </div>
          </form>
        )}
        {current && (
          <div className="card" style={{ overflowX: "auto" }}>
            <div className="section" style={{ padding: "16px 20px" }}>People with access ({data?.people.length ?? "…"})</div>
            <table>
              <thead><tr><th>User ID</th><th>Name</th><th>Role</th><th>Granted by</th><th>Granted on</th><th /></tr></thead>
              <tbody>
                {(data?.people || []).map((p) => (
                  <tr key={p.user_id}>
                    <td style={{ fontWeight: 600 }}>{p.user_id}</td>
                    <td>{p.display_name}</td>
                    <td>{p.role === "owner" ? <span className="badge">Owner</span> : <span className="badge g">User</span>}</td>
                    <td className={p.role === "owner" ? "muted" : ""}>{p.role === "owner" ? "Created the base" : p.granted_by}</td>
                    <td>{formatDate(p.granted_at)}</td>
                    <td>{p.role !== "owner" && <button className="act danger" onClick={() => revoke(p.user_id)}>Revoke</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div style={{ width: 416, display: "flex", flexDirection: "column", gap: 16 }}>
        <div>
          <label className="lbl" htmlFor="kb-select">Knowledge base</label>
          <select id="kb-select" className="inp" value={kb} onChange={(e) => { setMessage(null); setError(null); setParams({ kb: e.target.value }); }}>
            {(owned || []).map((k) => (
              <option key={k.kb_name} value={k.kb_name}>{k.kb_name} ({k.domain} / {k.sub_domain})</option>
            ))}
          </select>
        </div>
        {current && (
          <div className="card" style={{ padding: 20, minHeight: 300 }}>
            <div className="section">Access log</div>
            <div className="muted small" style={{ marginBottom: 8 }}>Every grant and revoke is written to Postgres.</div>
            {(data?.log || []).map((e, i) => (
              <div key={i} style={{ display: "flex", gap: 12, padding: "12px 0", borderBottom: "1px solid var(--line-soft)" }}>
                <span style={{ width: 8, height: 8, borderRadius: 4, marginTop: 6, background: DOT[e.action], flexShrink: 0 }} />
                <div style={{ fontSize: 13 }}>
                  <div>
                    <strong>{e.actor}</strong>{" "}
                    {e.action === "created" ? "created the knowledge base and became owner"
                      : e.action === "granted" ? <>granted <strong>{e.user_id}</strong> access</>
                      : <>revoked access for <strong>{e.user_id}</strong></>}
                  </div>
                  <div className="muted small">{formatDate(e.at, true)}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
