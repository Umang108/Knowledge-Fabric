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
      <div className="logo"><div className="mark" />Graphbase</div>
      <nav className="tabs" aria-label="Main">
        <NavLink className={tab} to="/workspace">Workspace</NavLink>
        <NavLink className={tab} to="/add-data">Add data</NavLink>
        <NavLink className={tab} to="/access">Access</NavLink>
        <NavLink className={tab} to="/chat">Chat</NavLink>
      </nav>
      <div className="user">
        <div className="av" aria-hidden="true">{initials}</div>
        <div>
          <div style={{ fontWeight: 600 }}>{user?.display_name}</div>
          <div className="muted" style={{ fontSize: 12 }}>{user?.user_id}</div>
        </div>
        <button className="tab" onClick={signOut}>Sign out</button>
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
