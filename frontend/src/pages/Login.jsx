import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { authConfig, currentUser, keycloakLogin, localLogin } from "../auth.js";

function Network() {
  // decorative graph from the mockup
  const nodes = [[120, 300, 14], [290, 380, 22], [470, 290, 16], [620, 410, 12], [90, 480, 10], [330, 540, 18], [560, 560, 15]];
  const edges = [[0, 1], [1, 2], [2, 3], [0, 4], [4, 5], [1, 5], [5, 6], [6, 3]];
  return (
    <svg viewBox="40 250 640 340" style={{ width: "100%", maxWidth: 600 }} aria-hidden="true">
      {edges.map(([a, b], i) => (
        <line key={i} x1={nodes[a][0]} y1={nodes[a][1]} x2={nodes[b][0]} y2={nodes[b][1]} stroke="#5fb8a8" strokeOpacity="0.55" />
      ))}
      {nodes.map(([x, y, r], i) => (
        <g key={i}>
          <circle cx={x} cy={y} r={r} fill="#0d5048" stroke="#7fd3c1" strokeWidth="2" />
          {i === 1 && <circle cx={x} cy={y} r={7} fill="#7fd3c1" />}
        </g>
      ))}
    </svg>
  );
}

export default function Login() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = params.get("next") || "/workspace";
  const [cfg, setCfg] = useState(null);
  const [userId, setUserId] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(params.get("error"));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    currentUser().then((u) => u && navigate(next, { replace: true })).catch(() => {});
    authConfig().then(setCfg).catch(() => setError("The server is not reachable"));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (cfg.provider === "keycloak") {
        keycloakLogin(next);
        return;
      }
      await localLogin(userId.trim(), password);
      navigate(next, { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="split login-shell" style={{ display: "flex", minHeight: "100vh" }}>
      <div className="login-showcase" style={{ width: "50%", background: "#0d5048", color: "#fff", padding: "56px 72px", display: "flex",
                    flexDirection: "column", justifyContent: "space-between" }}>
        <div className="logo" style={{ color: "#fff" }}><div className="mark" style={{ background: "#7fd3c1" }} />TCS Knowledge Fabric</div>
        <Network />
        <div className="login-copy">
          <div className="login-eyebrow">KNOWLEDGE WORKSPACE</div>
          <div style={{ fontSize: 40, fontWeight: 700, lineHeight: 1.15, maxWidth: 540 }}>
            Turn spreadsheets and documents into answers you can trace.
          </div>
          <div style={{ marginTop: 20, color: "#bfe3db", fontSize: 16, lineHeight: 1.5, maxWidth: 520 }}>
            Build knowledge graphs and RAG stores, share them with your team, and chat with the data you have access to.
          </div>
        </div>
      </div>
      <div className="login-form-side" style={{ width: "50%", display: "flex", alignItems: "center", justifyContent: "center", padding: 32 }}>
        <form className="login-form" onSubmit={submit} style={{ width: 400, display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <div className="login-form-kicker">WELCOME BACK</div>
            <h1>Sign in</h1>
            <div className="sub">
              {cfg?.provider === "keycloak" ? "Use your organisation account. You will be sent to the company sign-in page."
                                            : "Use your organisation user ID and password."}
            </div>
          </div>
          {cfg?.provider !== "keycloak" && (
            <>
              <div>
                <label className="lbl req" htmlFor="uid">User ID</label>
                <input id="uid" className="inp" autoComplete="username" value={userId}
                       onChange={(e) => setUserId(e.target.value)} required autoFocus />
              </div>
              <div>
                <label className="lbl req" htmlFor="pw">Password</label>
                <input id="pw" className="inp" type="password" autoComplete="current-password" value={password}
                       onChange={(e) => setPassword(e.target.value)} required />
              </div>
            </>
          )}
          {params.get("expired") && !error && (
            <div className="notice" role="status">Your session ended. Please sign in again.</div>
          )}
          {error && <div className="error" role="alert">{error}</div>}
          <button className="btn" type="submit" disabled={!cfg || busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
          <div className="muted small" style={{ textAlign: "center" }}>Need an account? Ask your administrator.</div>
        </form>
      </div>
    </div>
  );
}
