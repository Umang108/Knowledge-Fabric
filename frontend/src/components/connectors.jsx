// SAP / ServiceNow connections: saving and testing them, and choosing the tables (datasets) to pull.
import { useCallback, useEffect, useState } from "react";
import { api, formatDate } from "../api.js";
import { ErrorBox, Modal } from "./common.jsx";

export const CUSTOM = "custom";

export function useConnectors() {
  const [kinds, setKinds] = useState([]);
  const [connections, setConnections] = useState(null);
  const [error, setError] = useState(null);
  const reload = useCallback(
    () =>
      Promise.all([api("/connectors"), api("/connections")])
        .then(([k, c]) => {
          setKinds(k.kinds);
          setConnections(c);
        })
        .catch(setError),
    [],
  );
  useEffect(() => {
    reload();
  }, [reload]);
  return { kinds, connections, reload, error };
}

const EMPTY = {
  name: "",
  kind: "servicenow",
  base_url: "",
  auth_type: "basic",
  username: "",
  secret: "",
  options: { verify_tls: true },
};

const PLACEHOLDER_URL = {
  servicenow: "https://yourcompany.service-now.com",
  sap: "https://s4.yourcompany.com:44300",
};

export function ConnectionForm({ kinds, initial, onSaved, onCancel }) {
  const editing = !!initial?.id;
  const [c, setC] = useState(() =>
    initial
      ? { ...EMPTY, ...initial, secret: "", options: { verify_tls: true, ...initial.options } }
      : EMPTY,
  );
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => setC((x) => ({ ...x, [k]: v }));
  const opt = (k, v) => setC((x) => ({ ...x, options: { ...x.options, [k]: v } }));
  const isSap = c.kind === "sap";
  const oauth = c.auth_type === "oauth";

  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const options = Object.fromEntries(
      Object.entries(c.options).filter(([, v]) => v !== "" && v !== null && v !== undefined),
    );
    const body = {
      name: c.name.trim(),
      kind: c.kind,
      base_url: c.base_url.trim(),
      auth_type: c.auth_type,
      username: oauth ? null : c.username.trim(),
      options,
    };
    if (c.secret) body.secret = c.secret;
    try {
      const saved = editing
        ? await api(`/connections/${initial.id}`, { method: "PUT", json: body })
        : await api("/connections", { method: "POST", json: body });
      const test = await api(`/connections/${saved.id}/test`, { method: "POST" });
      onSaved(saved, test);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={save} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="row">
        <div className="grow">
          <label className="lbl" htmlFor="cn-name">Connection name</label>
          <input id="cn-name" className="inp" value={c.name} required maxLength={100}
                 placeholder="e.g. ServiceNow production" onChange={(e) => set("name", e.target.value)} />
        </div>
        <div style={{ width: 170 }}>
          <label className="lbl" htmlFor="cn-kind">System</label>
          <select id="cn-kind" className="inp" value={c.kind} disabled={editing}
                  onChange={(e) => set("kind", e.target.value)}>
            {kinds.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
          </select>
        </div>
      </div>
      <div>
        <label className="lbl" htmlFor="cn-url">Base URL</label>
        <input id="cn-url" className="inp" value={c.base_url} required type="url"
               placeholder={PLACEHOLDER_URL[c.kind]} onChange={(e) => set("base_url", e.target.value)} />
      </div>
      {isSap && (
        <div className="row">
          <div className="grow">
            <label className="lbl" htmlFor="cn-client">SAP client (optional)</label>
            <input id="cn-client" className="inp" value={c.options.sap_client || ""} placeholder="e.g. 100"
                   onChange={(e) => opt("sap_client", e.target.value)} />
          </div>
          <div className="grow">
            <label className="lbl" htmlFor="cn-odata">OData version</label>
            <select id="cn-odata" className="inp" value={String(c.options.odata_version || "2")}
                    onChange={(e) => opt("odata_version", e.target.value)}>
              <option value="2">V2 (/sap/opu/odata/sap/)</option>
              <option value="4">V4 (/sap/opu/odata4/sap/)</option>
            </select>
          </div>
        </div>
      )}
      <div className="row">
        <div style={{ width: 200 }}>
          <label className="lbl" htmlFor="cn-auth">Authentication</label>
          <select id="cn-auth" className="inp" value={c.auth_type} onChange={(e) => set("auth_type", e.target.value)}>
            <option value="basic">User + password</option>
            <option value="oauth">OAuth client credentials</option>
          </select>
        </div>
        {oauth ? (
          <div className="grow">
            <label className="lbl" htmlFor="cn-cid">Client ID</label>
            <input id="cn-cid" className="inp" value={c.options.client_id || ""} required
                   onChange={(e) => opt("client_id", e.target.value)} />
          </div>
        ) : (
          <div className="grow">
            <label className="lbl" htmlFor="cn-user">{isSap ? "Communication user" : "Integration user"}</label>
            <input id="cn-user" className="inp" value={c.username || ""} required autoComplete="off"
                   onChange={(e) => set("username", e.target.value)} />
          </div>
        )}
      </div>
      {oauth && (
        <div>
          <label className="lbl" htmlFor="cn-token">
            Token URL{isSap ? "" : " (optional, defaults to /oauth_token.do)"}
          </label>
          <input id="cn-token" className="inp" value={c.options.token_url || ""} required={isSap}
                 placeholder={isSap ? "https://<subaccount>.authentication.<region>.hana.ondemand.com/oauth/token" : ""}
                 onChange={(e) => opt("token_url", e.target.value)} />
        </div>
      )}
      <div>
        <label className="lbl" htmlFor="cn-secret">{oauth ? "Client secret" : "Password"}</label>
        <input id="cn-secret" className="inp" type="password" value={c.secret} autoComplete="new-password"
               required={!editing} placeholder={editing ? "Leave empty to keep the saved one" : ""}
               onChange={(e) => set("secret", e.target.value)} />
        <div className="muted small" style={{ marginTop: 4 }}>
          Stored encrypted on the server and never sent back to the browser.
        </div>
      </div>
      <div className="row" style={{ alignItems: "center" }}>
        <div style={{ width: 200 }}>
          <label className="lbl" htmlFor="cn-max">Max rows per table</label>
          <input id="cn-max" className="inp" type="number" min={1} value={c.options.max_rows || ""}
                 placeholder="50000" onChange={(e) => opt("max_rows", e.target.value)} />
        </div>
        <label className="grow" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 20 }}>
          <input type="checkbox" checked={c.options.verify_tls !== false}
                 onChange={(e) => opt("verify_tls", e.target.checked)} />
          Verify the TLS certificate (keep on; use CONNECTOR_CA_BUNDLE for an internal CA)
        </label>
      </div>
      <ErrorBox error={error} />
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 12 }}>
        <button className="btn sec" type="button" onClick={onCancel}>Cancel</button>
        <button className="btn" type="submit" disabled={busy}>{busy ? "Saving and testing…" : "Save and test"}</button>
      </div>
    </form>
  );
}

function TestBadge({ c }) {
  if (c.last_test_ok === true) return <span className="badge" title={c.last_test_detail}>Connected</span>;
  if (c.last_test_ok === false) return <span className="badge r" title={c.last_test_detail}>Test failed</span>;
  return <span className="badge g">Not tested</span>;
}

export function ConnectionsModal({ kinds, connections, reload, onClose }) {
  const [editing, setEditing] = useState(null); // null | {} (new) | connection
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [testing, setTesting] = useState(null);

  const test = async (c) => {
    setTesting(c.id);
    setError(null);
    try {
      const r = await api(`/connections/${c.id}/test`, { method: "POST" });
      setMessage({ ok: r.ok, text: `${c.name}: ${r.detail}` });
      reload();
    } catch (err) {
      setError(err);
    } finally {
      setTesting(null);
    }
  };
  const remove = async (c) => {
    if (!window.confirm(`Delete the connection "${c.name}"? Knowledge bases built from it are kept.`)) return;
    try {
      await api(`/connections/${c.id}`, { method: "DELETE" });
      reload();
    } catch (err) {
      setError(err);
    }
  };

  return (
    <Modal title={editing ? (editing.id ? `Edit ${editing.name}` : "New connection") : "Connections"} onClose={onClose}>
      {editing ? (
        <ConnectionForm
          kinds={kinds}
          initial={editing.id ? editing : null}
          onCancel={() => setEditing(null)}
          onSaved={(saved, t) => {
            setMessage({ ok: t.ok, text: `${saved.name}: ${t.detail}` });
            setEditing(null);
            reload();
          }}
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {message && (
            <div className={message.ok ? "notice" : "error"} role="status">{message.text}</div>
          )}
          <ErrorBox error={error} />
          <table>
            <thead>
              <tr><th>Name</th><th>System</th><th>URL</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {connections?.length === 0 && (
                <tr><td colSpan={5} className="muted">No connections yet.</td></tr>
              )}
              {(connections || []).map((c) => (
                <tr key={c.id}>
                  <td style={{ fontWeight: 600 }}>{c.name}</td>
                  <td>{c.label}</td>
                  <td className="small" style={{ wordBreak: "break-all" }}>{c.base_url}</td>
                  <td>
                    <TestBadge c={c} />
                    {c.last_tested_at && <div className="muted small">{formatDate(c.last_tested_at, true)}</div>}
                  </td>
                  <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                    <button className="btn sec sm" onClick={() => test(c)} disabled={testing === c.id}>
                      {testing === c.id ? "Testing…" : "Test"}
                    </button>{" "}
                    <button className="btn sec sm" onClick={() => setEditing(c)}>Edit</button>{" "}
                    <button className="btn danger sm" onClick={() => remove(c)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button className="btn" onClick={() => { setMessage(null); setEditing({}); }}>Add connection</button>
          </div>
        </div>
      )}
    </Modal>
  );
}

// ------------------------------------------------------------------ datasets
const asRow = (d) => ({
  include: true,
  name: d.name || "",
  source: d.source || "",
  fields: (d.fields || []).join(", "),
  filter: d.filter || "",
  limit: d.limit ?? "",
  changed_field: d.changed_field || "",
});

export function presetsFor(kinds, connection) {
  return kinds.find((k) => k.kind === connection?.kind)?.presets || {};
}

// A fresh selection for a connection: its first preset (optionally only presets of one KB type).
export function initialSelection(kinds, connection, kbType) {
  const presets = presetsFor(kinds, connection);
  const key = Object.keys(presets).find((p) => !kbType || presets[p].kb_type === kbType);
  if (!key) return { preset: CUSTOM, kbType: kbType || "graph", datasets: [asRow({})] };
  return { preset: key, kbType: presets[key].kb_type, datasets: presets[key].datasets.map(asRow) };
}

export function datasetsPayload(selection) {
  return selection.datasets
    .filter((d) => d.include && d.source.trim())
    .map((d) => ({
      name: d.name.trim() || undefined,
      source: d.source.trim(),
      fields: d.fields.split(",").map((f) => f.trim()).filter(Boolean),
      filter: d.filter.trim(),
      limit: d.limit === "" ? null : Number(d.limit),
      changed_field: d.changed_field.trim(),
    }));
}

const SOURCE_HINT = {
  servicenow: "Table, e.g. incident",
  sap: "Service/EntitySet, e.g. API_SALES_ORDER_SRV/A_SalesOrder",
};
const FILTER_HINT = {
  servicenow: "Encoded query, e.g. active=true",
  sap: "OData $filter, e.g. SalesOrganization eq '1710'",
};

// kbType: restrict presets to one KB type (Add data on an existing KB); omit to let the preset decide.
export function DatasetPicker({ kinds, connection, kbType, value, onChange }) {
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState(null);
  const presets = presetsFor(kinds, connection);
  const keys = Object.keys(presets).filter((p) => !kbType || presets[p].kb_type === kbType);
  const kind = connection?.kind;

  const pickPreset = (key) => {
    if (key === CUSTOM) onChange({ preset: CUSTOM, kbType: kbType || "graph", datasets: [asRow({})] });
    else onChange({ preset: key, kbType: presets[key].kb_type, datasets: presets[key].datasets.map(asRow) });
  };
  const edit = (i, k, v) =>
    onChange({ ...value, datasets: value.datasets.map((d, j) => (j === i ? { ...d, [k]: v } : d)) });
  const add = () => onChange({ ...value, datasets: [...value.datasets, asRow({})] });
  const drop = (i) => onChange({ ...value, datasets: value.datasets.filter((_, j) => j !== i) });

  const show = async (d) => {
    setError(null);
    try {
      const [ds] = datasetsPayload({ datasets: [{ ...d, include: true }] });
      setPreview(await api(`/connections/${connection.id}/preview`, { method: "POST", json: { dataset: ds } }));
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row">
        <div className="grow">
          <label className="lbl" htmlFor="ds-preset">What to pull</label>
          <select id="ds-preset" className="inp" value={value.preset} onChange={(e) => pickPreset(e.target.value)}>
            {keys.map((k) => <option key={k} value={k}>{presets[k].label}</option>)}
            <option value={CUSTOM}>Custom tables</option>
          </select>
        </div>
        {!kbType && value.preset === CUSTOM && kind === "servicenow" && (
          <div style={{ width: 200 }}>
            <label className="lbl" htmlFor="ds-type">Build</label>
            <select id="ds-type" className="inp" value={value.kbType}
                    onChange={(e) => onChange({ ...value, kbType: e.target.value })}>
              <option value="graph">Knowledge graph</option>
              <option value="rag">RAG store (articles)</option>
            </select>
          </div>
        )}
      </div>
      <div className="muted small">
        Untick a table to leave it out. Fields are comma-separated (empty = every field the API returns).
        {kind === "sap" && " Set the changed field to allow “only changes since” pulls."}
      </div>
      {value.datasets.map((d, i) => (
        <div key={i} className={`ds-row${d.include ? "" : " off"}`}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={d.include} aria-label={`Include ${d.name || d.source || "table"}`}
                   onChange={(e) => edit(i, "include", e.target.checked)} />
            <input className="inp sm" style={{ width: 170, fontWeight: 600 }} value={d.name} placeholder="Sheet name"
                   aria-label="Sheet name" onChange={(e) => edit(i, "name", e.target.value)} />
            <input className="inp sm" value={d.source} placeholder={SOURCE_HINT[kind]} aria-label="Source"
                   onChange={(e) => edit(i, "source", e.target.value)} />
          </div>
          <input className="inp sm" value={d.fields} placeholder="Fields" aria-label="Fields"
                 onChange={(e) => edit(i, "fields", e.target.value)} />
          <div style={{ display: "flex", gap: 6 }}>
            <input className="inp sm" value={d.filter} placeholder={FILTER_HINT[kind]} aria-label="Filter"
                   onChange={(e) => edit(i, "filter", e.target.value)} />
            {kind === "sap" && (
              <input className="inp sm" style={{ width: 150 }} value={d.changed_field} aria-label="Changed field"
                     placeholder="Changed field" onChange={(e) => edit(i, "changed_field", e.target.value)} />
            )}
            <input className="inp sm" style={{ width: 90 }} type="number" min={1} value={d.limit}
                   placeholder="Limit" aria-label="Limit" onChange={(e) => edit(i, "limit", e.target.value)} />
            <button type="button" className="btn sec sm" disabled={!d.source.trim()} onClick={() => show(d)}>
              Preview
            </button>
            <button type="button" className="btn danger sm" aria-label="Remove table" onClick={() => drop(i)}>
              ×
            </button>
          </div>
        </div>
      ))}
      <div>
        <button type="button" className="btn link" onClick={add}>+ Add a table</button>
      </div>
      <ErrorBox error={error} />
      {preview && (
        <Modal title={`Preview: ${preview.name} (first ${preview.rows.length} rows)`} onClose={() => setPreview(null)}>
          <div style={{ overflowX: "auto" }}>
            <table>
              <thead><tr>{preview.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {preview.rows.map((r, i) => (
                  <tr key={i}>{preview.columns.map((c) => <td key={c} className="small">{String(r[c] ?? "")}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
          {preview.rows.length === 0 && <div className="muted">No rows matched.</div>}
        </Modal>
      )}
    </div>
  );
}
