import { useCallback, useEffect, useState } from "react";
import { api, formatDate } from "../api.js";
import { ErrorBox } from "../components/common.jsx";

// Signed-in browsers/devices of the current user; sign out any of them or all the others.
export default function Sessions() {
  const [sessions, setSessions] = useState(null);
  const [timing, setTiming] = useState(null);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);

  const load = useCallback(() => {
    api("/auth/sessions").then(setSessions).catch(setError);
    api("/auth/session").then(setTiming).catch(() => {});
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const revoke = async (s) => {
    if (s.current && !window.confirm("Sign out of this browser?")) return;
    try {
      const r = await api(`/auth/sessions/${s.id}/revoke`, { method: "POST" });
      if (r.current) window.location.assign("/login");
      else {
        setMessage(`Signed out ${s.device}.`);
        load();
      }
    } catch (e) {
      setError(e);
    }
  };
  const revokeOthers = async () => {
    try {
      const r = await api("/auth/sessions/revoke-others", { method: "POST" });
      setMessage(`Signed out of ${r.revoked} other session${r.revoked === 1 ? "" : "s"}.`);
      load();
    } catch (e) {
      setError(e);
    }
  };

  const others = (sessions || []).filter((s) => !s.current).length;
  return (
    <div className="page">
      <div>
        <h1>Sessions</h1>
        <div className="sub">
          Where you are signed in.
          {timing && ` You are signed out after ${timing.idle_minutes} minutes without activity, and after
          ${timing.max_hours} hours in any case.`}
        </div>
      </div>
      {message && <div className="notice" role="status">{message}</div>}
      <ErrorBox error={error} />
      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: "26%" }}>Browser / device</th>
              <th>Sign-in</th>
              <th>IP address</th>
              <th>Signed in</th>
              <th>Last active</th>
              <th>Ends</th>
              <th style={{ width: 110 }} />
            </tr>
          </thead>
          <tbody>
            {(sessions || []).map((s) => (
              <tr key={s.id}>
                <td>
                  <strong>{s.device}</strong>
                  {s.current && <span className="badge" style={{ marginLeft: 8 }}>This browser</span>}
                </td>
                <td>{s.auth_source === "keycloak" ? "Keycloak" : "Password"}</td>
                <td className="mono small">{s.ip_address || "—"}</td>
                <td>{formatDate(s.signed_in_at, true)}</td>
                <td>{formatDate(s.last_seen_at, true)}</td>
                <td>{formatDate(s.expires_at, true)}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="act danger" onClick={() => revoke(s)}>Sign out</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <button className="btn sec" onClick={revokeOthers} disabled={!others}>
          Sign out of all other sessions{others ? ` (${others})` : ""}
        </button>
      </div>
    </div>
  );
}
