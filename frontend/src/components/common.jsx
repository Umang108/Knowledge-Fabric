import { createContext, useContext, useEffect, useRef, useState } from "react";
import { NavLink } from "react-router-dom";
import { signOut } from "../auth.js";
import { STATUS_TEXT } from "../api.js";

export const UserContext = createContext(null);
export const useUser = () => useContext(UserContext);

export function TopBar() {
  const user = useUser();
  const initials = (user?.display_name || user?.user_id || "?")
    .split(/[\s.]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  const tab = ({ isActive }) => `tab${isActive ? " on" : ""}`;
  return (
    <header className="top">
      <NavLink className="logo" to="/workspace" aria-label="TCS Knowledge Fabric home">
        <div className="mark" />
        <span>TCS Knowledge Fabric</span>
      </NavLink>
      <nav className="tabs" aria-label="Main">
        <NavLink className={tab} to="/workspace">Workspace</NavLink>
        <NavLink className={tab} to="/add-data">Add data</NavLink>
        <NavLink className={tab} to="/access">Access</NavLink>
        <NavLink className={tab} to="/chat">Chat</NavLink>
      </nav>
      <div className="user">
        <div className="av" aria-hidden="true">{initials}</div>
        <div className="user-details">
          <div style={{ fontWeight: 600 }}>{user?.display_name}</div>
          <div className="muted" style={{ fontSize: 12 }}>{user?.user_id}</div>
        </div>
        <div className="user-actions">
          <NavLink className={tab} to="/sessions" title="Signed-in devices and session timeout">Sessions</NavLink>
          <button className="tab" onClick={signOut}>Sign out</button>
        </div>
      </div>
    </header>
  );
}

export function TypeBadge({ type }) {
  return type === "graph" ? <span className="badge b">Graph</span> : <span className="badge g">RAG</span>;
}

export function RoleBadge({ role, status }) {
  if (role === "owner" && status && status !== "ready") return <span className="badge w">Draft</span>;
  return role === "owner" ? <span className="badge">Owner</span> : <span className="badge g">User</span>;
}

export function StatusText({ kb }) {
  const text = STATUS_TEXT[kb.status] || kb.status;
  if (kb.status === "failed") {
    return <span style={{ color: "var(--red)" }} title={kb.status_detail || ""}>{text}</span>;
  }
  return <span>{text}</span>;
}

export function DropZone({ accept, multiple, onFiles, title, hint }) {
  const input = useRef(null);
  const [over, setOver] = useState(false);
  const pick = (list) => {
    const files = Array.from(list || []);
    if (files.length) onFiles(multiple ? files : [files[0]]);
  };
  return (
    <div
      className={`drop${over ? " over" : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => input.current.click()}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current.click()}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files); }}
    >
      <div style={{ fontWeight: 600 }}>{title}</div>
      <div className="muted">or <span style={{ color: "var(--teal)", fontWeight: 600 }}>browse</span>{hint}</div>
      <input ref={input} type="file" hidden accept={accept} multiple={multiple}
             onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
    </div>
  );
}

// Poll fn every `ms` while `active` is true.
export function usePoll(fn, ms, active, deps = []) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(() => saved.current(), ms);
    return () => clearInterval(id);
  }, [active, ms, ...deps]); // eslint-disable-line react-hooks/exhaustive-deps
}

export function ErrorBox({ error }) {
  if (!error) return null;
  return <div className="error" role="alert">{String(error.message || error)}</div>;
}

export function Modal({ title, onClose, children }) {
  useEffect(() => {
    const esc = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="overlay" onClick={onClose}>
      <div className="card modal" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <div style={{ fontSize: 18, fontWeight: 600 }}>{title}</div>
          <button className="btn sec sm" onClick={onClose}>Close</button>
        </div>
        {children}
      </div>
    </div>
  );
}

// A progress bar that always shows its percentage.
export function ProgressBar({ value, label = "progress", style }) {
  const pct = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  return (
    <div className="progress" style={style}>
      <div className="bar" role="progressbar" aria-label={label} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div style={{ width: `${pct}%` }} />
      </div>
      <span className="pct">{pct}%</span>
    </div>
  );
}

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

export function CopyButton({ text, label = "Copy", className = "iconbtn" }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const t = document.createElement("textarea");  // http:// pages have no clipboard API
      t.value = text;
      document.body.appendChild(t);
      t.select();
      document.execCommand("copy");
      t.remove();
    }
    setDone(true);
    setTimeout(() => setDone(false), 1500);
  };
  return (
    <button type="button" className={className} onClick={copy} aria-label={`${label}: ${text.slice(0, 40)}`}>
      <CopyIcon />{done ? "Copied" : label}
    </button>
  );
}

// Cypher block: minimised by default; Show expands it in place, Maximise opens it full screen.
export function CypherPanel({ title, code, meta, defaultOpen = false, preview }) {
  const [open, setOpen] = useState(defaultOpen);
  const [max, setMax] = useState(false);
  const text = Array.isArray(code) ? code.join("\n") : code || "";
  const count = Array.isArray(code) ? code.length : text.split("\n").filter(Boolean).length;
  return (
    <>
      <div className={`code${open ? "" : " collapsed"}`}>
        <div className="head" style={{ marginBottom: open ? 8 : 0 }}>
          <span>{title}{meta ? ` · ${meta}` : ""}</span>
          <span className="tools">
            <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}>
              {open ? "Minimise" : `Show${count > 1 ? ` (${count} statements)` : ""}`}
            </button>
            <button type="button" onClick={() => setMax(true)}>Maximise</button>
          </span>
        </div>
        {open && <pre>{preview ? preview(text) : text}</pre>}
      </div>
      {max && (
        <div className="overlay full" onClick={() => setMax(false)}>
          <div className="card modal wide" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <div style={{ fontSize: 18, fontWeight: 600 }}>{title}</div>
              <div style={{ display: "flex", gap: 10 }}>
                <CopyButton text={text} />
                <button className="btn sec sm" onClick={() => setMax(false)}>Close</button>
              </div>
            </div>
            <div className="code"><pre>{text}</pre></div>
          </div>
        </div>
      )}
    </>
  );
}

// Warns before an idle session times out and sends the user to sign-in when it has.
const WARN_SECONDS = 120;
export function SessionWatcher() {
  const [left, setLeft] = useState(null);       // seconds until the session ends, when it's close
  const timing = useRef(null);                   // {offset, idle, absolute}
  const check = useRef(null);
  check.current = async () => {
    const r = await fetch("/api/auth/session", { credentials: "same-origin" }).catch(() => null);
    if (!r) return;
    if (r.status === 401) {
      window.location.assign(`/login?expired=1&next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
      return;
    }
    const t = await r.json();
    timing.current = {
      offset: Date.now() - new Date(t.server_time).getTime(),
      end: Math.min(new Date(t.idle_expires_at).getTime(), new Date(t.expires_at).getTime()),
    };
  };
  useEffect(() => {
    check.current();
    const poll = setInterval(() => check.current(), 30000);
    const tick = setInterval(() => {
      if (!timing.current) return;
      const secs = Math.round((timing.current.end + timing.current.offset - Date.now()) / 1000);
      if (secs <= 0) check.current();
      setLeft(secs <= WARN_SECONDS ? Math.max(secs, 0) : null);
    }, 1000);
    return () => { clearInterval(poll); clearInterval(tick); };
  }, []);
  if (left == null) return null;
  const stay = async () => {
    await fetch("/api/auth/me", { credentials: "same-origin" });  // any request counts as activity
    await check.current();
    setLeft(null);
  };
  const mm = String(Math.floor(left / 60)).padStart(1, "0");
  const ss = String(left % 60).padStart(2, "0");
  return (
    <div className="overlay">
      <div className="card modal" role="alertdialog" aria-label="Session expiring" style={{ maxWidth: 440 }}>
        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>Your session is about to end</div>
        <div className="muted" style={{ marginBottom: 18 }}>
          You will be signed out in <strong>{mm}:{ss}</strong> because of inactivity.
        </div>
        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
          <button className="btn sec" onClick={signOut}>Sign out</button>
          <button className="btn" onClick={stay}>Stay signed in</button>
        </div>
      </div>
    </div>
  );
}
