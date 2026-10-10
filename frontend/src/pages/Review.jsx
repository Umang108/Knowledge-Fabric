import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import { CypherPanel, ErrorBox } from "../components/common.jsx";

const clone = (x) => JSON.parse(JSON.stringify(x));
// short labels for badges; the category picker uses the NIST catalogue labels from the server
const PII_LABEL = { person_name: "name", email: "email", phone: "phone", address: "address", date_of_birth: "DOB",
                    government_id: "gov ID", bank_account: "account", personal_id: "personal ID", biometric: "biometric",
                    online_identifier: "device ID", demographic: "demographic", health: "health",
                    financial: "financial", location: "location", free_text: "free text", other: "other" };
let CATEGORY_LABEL = { ...PII_LABEL };
const LEVEL = { low: "Low", moderate: "Moderate", high: "High" };
const PII_CATEGORIES = Object.keys(PII_LABEL);
const SOURCE_LABEL = { llm: "LLM", rules: "rules", user: "you" };
const piiKey = (sheet, column) => `${sheet}|${column}`;
const piiActive = (entry) => !!entry && entry.status !== "dismissed";

// Mark / unmark / recategorise one column. Unmarking something the LLM or the rules found keeps it as
// "dismissed" (the decision is recorded); unmarking something only a person had marked removes it.
function applyPii(list, sheet, column, change) {
  const idx = list.findIndex((p) => piiKey(p.sheet, p.column) === piiKey(sheet, column));
  const cur = idx >= 0 ? list[idx] : null;
  const next = list.slice();
  if (change.mark === false) {
    if (!cur) return list;
    if (cur.detected_by === "user") next.splice(idx, 1);
    else next[idx] = { ...cur, status: "dismissed" };
    return next;
  }
  const category = change.category || cur?.category || "other";
  const entry = cur
    ? { ...cur, category, status: "confirmed" }
    : { sheet, column, category, status: "confirmed", detected_by: "user", confidence: 1,
        reason: "Marked as PII on the Review screen" };
  if (idx >= 0) next[idx] = entry; else next.push(entry);
  return next;
}

function PiiControl({ entry, label, categories, disabled, onChange }) {
  const on = piiActive(entry);
  return (
    <span className="pii-ctl">
      <label className="small" title={entry ? `Found by ${SOURCE_LABEL[entry.detected_by] || entry.detected_by}` : ""}>
        <input type="checkbox" checked={on} disabled={disabled} aria-label={`PII ${label}`}
               onChange={(e) => onChange({ mark: e.target.checked })} /> PII
      </label>
      {on && (
        <select className="inp sm" aria-label={`PII category ${label}`} value={entry.category} disabled={disabled}
                onChange={(e) => onChange({ category: e.target.value })}>
          {categories.map((c) => <option key={c} value={c}>{CATEGORY_LABEL[c] || c}</option>)}
        </select>
      )}
    </span>
  );
}

function PropList({ props, pii, sheet }) {
  if (!props.length) return <span className="muted">none</span>;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
      {props.map((p, i) => {
        const hit = pii.get(piiKey(sheet, p.column));
        return (
          <span key={p.name + i} className="chip" title={`Stored as "${p.name}" from column "${p.column}" (${p.type})`}>
            {p.name}
            {piiActive(hit) && <span className="badge pii" title={`PII: ${hit.category} (found by ${SOURCE_LABEL[hit.detected_by] || hit.detected_by})`}>PII {PII_LABEL[hit.category] || ""}</span>}
          </span>
        );
      })}
    </div>
  );
}

function PropEditor({ props, onChange }) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
      {props.map((p, i) => (
        <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 2 }}>
          <input className="inp sm" style={{ width: 120 }} value={p.name} aria-label={`Property ${p.column}`}
                 onChange={(e) => onChange(props.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
          <button type="button" className="btn sec sm" style={{ padding: "0 8px" }} aria-label={`Remove ${p.name}`}
                  onClick={() => onChange(props.filter((_, j) => j !== i))}>×</button>
        </span>
      ))}
      {!props.length && <span className="muted small">no properties</span>}
    </div>
  );
}

// Every column that ends up stored in the graph, with where it is stored.
function storedColumns(nodes, relationships) {
  const out = new Map();
  const add = (sheet, column, where) => {
    const k = piiKey(sheet, column);
    if (!out.has(k)) out.set(k, { sheet, column, where: [] });
    out.get(k).where.push(where);
  };
  for (const n of nodes) {
    add(n.sheet, n.key.column, `${n.label}.${n.key.name} (key)`);
    for (const p of n.properties || []) add(n.sheet, p.column, `${n.label}.${p.name}`);
  }
  for (const r of relationships) for (const p of r.properties || []) add(r.sheet, p.column, `${r.type}.${p.name}`);
  return [...out.values()];
}

function Impact({ entry }) {
  if (!entry?.nist_impact) return <span className="muted">—</span>;
  const tip = [...(entry.nist_factors || []), "", "Safeguards (NIST Privacy Framework):", ...(entry.nist_controls || [])].join("\n");
  return (
    <span title={tip}>
      <span className={`impact ${entry.nist_impact}`}>{LEVEL[entry.nist_impact]}</span>
      <div className="muted small">{entry.nist_identifier === "direct" ? "Direct identifier" : "Linkable"}</div>
    </span>
  );
}

function PiiPanel({ work, effective, assessed, categories, disabled, onPii }) {
  const [showAll, setShowAll] = useState(false);
  const byKey = new Map((work.pii || []).map((p) => [piiKey(p.sheet, p.column), p]));
  const stored = storedColumns(effective.schema.nodes, effective.schema.relationships);
  const rows = stored.filter((c) => showAll || byKey.has(piiKey(c.sheet, c.column)));
  const active = (work.pii || []).filter(piiActive).length;
  return (
    <div className="card panel">
      <div className="panel-head">
        <div>
          <div className="section">Personal data (PII) · NIST SP 800-122</div>
          <div className="help">
            {active} column{active === 1 ? "" : "s"} marked as PII. Each is classified as a direct identifier or
            linkable information and given a confidentiality impact level (Low / Moderate / High) from its
            sensitivity, what it is stored with and how many records hold it; hover the level for the factors and the
            NIST Privacy Framework safeguards. Tick or untick to decide yourself; your choice is saved with the graph
            and recorded in kb_pii_fields.
          </div>
        </div>
        <label className="small" style={{ whiteSpace: "nowrap" }}>
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)}
                 aria-label="Show all stored columns" /> Show all stored columns
        </label>
      </div>
      <div className="panel-body">
      <table>
        <thead><tr><th>Column (sheet)</th><th>Stored in the graph as</th><th style={{ width: 260 }}>PII category</th>
          <th style={{ width: 150 }}>NIST impact</th><th>Found by</th><th style={{ width: 110 }}>Status</th></tr></thead>
        <tbody>
          {!rows.length && (
            <tr><td colSpan={6} className="muted">No PII detected. Tick &ldquo;Show all stored columns&rdquo; to mark columns yourself.</td></tr>
          )}
          {rows.map((c) => {
            const entry = byKey.get(piiKey(c.sheet, c.column));
            const status = entry?.status || "";
            return (
              <tr key={piiKey(c.sheet, c.column)} data-pii-row={piiKey(c.sheet, c.column)}>
                <td><strong>{c.column}</strong><div className="muted small">{c.sheet}</div></td>
                <td className="mono small">{c.where.join(", ")}</td>
                <td><PiiControl entry={entry} label={c.column} categories={categories} disabled={disabled}
                                onChange={(change) => onPii(c.sheet, c.column, change)} /></td>
                <td>{piiActive(entry) ? <Impact entry={assessed.get(piiKey(c.sheet, c.column)) || entry} /> : <span className="muted">—</span>}</td>
                <td className="small">
                  {entry ? `${SOURCE_LABEL[entry.detected_by] || entry.detected_by}` : "—"}
                  {entry && entry.detected_by !== "user" && entry.confidence != null &&
                    <span className="muted"> ({Math.round(entry.confidence * 100)}%)</span>}
                </td>
                <td>
                  {status === "dismissed" && <span className="badge g">Not PII</span>}
                  {status === "confirmed" && <span className="badge">Confirmed</span>}
                  {status === "detected" && <span className="badge w">Detected</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
    </div>
  );
}

function Actions({ removed, editing, onEdit, onDelete, onUndo, onSave, onCancel }) {
  if (removed) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-start" }}>
        <span className="badge r">Removed</span>
        <button className="act" onClick={onUndo}>Undo</button>
      </div>
    );
  }
  if (editing) {
    return (
      <div className="acts">
        <button className="act" onClick={onSave}>Save</button>
        <button className="act" onClick={onCancel}>Cancel</button>
      </div>
    );
  }
  return (
    <div className="acts">
      <button className="act" onClick={onEdit}>Edit</button>
      <button className="act danger" onClick={onDelete}>Delete</button>
    </div>
  );
}

export default function Review() {
  const { kb } = useParams();
  const navigate = useNavigate();
  const [saved, setSaved] = useState(null);      // schema as extracted / last saved
  const [work, setWork] = useState(null);        // working copy
  const [removed, setRemoved] = useState({ nodes: new Set(), rels: new Set() });
  const [editing, setEditing] = useState(null);  // {kind, id, draft, pii}
  const [categories, setCategories] = useState(PII_CATEGORIES);
  const [adding, setAdding] = useState(null);    // "node" | "rel"
  const [preview, setPreview] = useState({ cypher: [], summary: null, errors: [], pii: [] });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api(`/kbs/${kb}/review`).then((r) => {
      const schema = { ...r.schema, pii: r.schema.pii || [] };
      setSaved(schema);
      setWork(clone(schema));
      if (r.pii_categories?.length) setCategories(r.pii_categories);
      if (r.pii_catalogue?.length) CATEGORY_LABEL = Object.fromEntries(r.pii_catalogue.map((c) => [c.category, c.label]));
      setPreview({ cypher: r.cypher, summary: r.summary, errors: [], pii: r.pii || [] });
    }).catch(setError);
  }, [kb]);

  const pii = useMemo(() => new Map((work?.pii || []).map((p) => [`${p.sheet}|${p.column}`, p])), [work]);
  // NIST fields come from the server (recomputed on every edit by the live preview)
  const assessed = useMemo(() => new Map((preview.pii || []).map((p) => [piiKey(p.sheet, p.column), p])), [preview]);

  // nodes removed explicitly take their relationships with them
  const effective = useMemo(() => {
    if (!work) return null;
    const gone = new Set(work.nodes.filter((n) => removed.nodes.has(n.id)).map((n) => n.label));
    const relGone = new Set(work.relationships
      .filter((r) => removed.rels.has(r.id) || gone.has(r.from.label) || gone.has(r.to.label)).map((r) => r.id));
    return {
      relGone,
      schema: {
        nodes: work.nodes.filter((n) => !removed.nodes.has(n.id)),
        relationships: work.relationships.filter((r) => !relGone.has(r.id)),
        pii: work.pii || [],
      },
    };
  }, [work, removed]);

  // live Cypher preview (debounced)
  const timer = useRef(null);
  useEffect(() => {
    if (!effective || !saved) return;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api(`/kbs/${kb}/review/preview`, { method: "POST", json: { schema: effective.schema } })
        .then((r) => setPreview({ cypher: r.cypher, summary: r.summary, errors: [], pii: r.pii || [] }))
        .catch((e) => setPreview((p) => ({ ...p, errors: e.details?.errors || [e.message] })));
    }, 350);
    return () => clearTimeout(timer.current);
  }, [effective, kb, saved]);

  const counts = useMemo(() => {
    if (!work || !saved) return { edits: 0, removals: 0, pii: 0 };
    const before = new Map([...saved.nodes, ...saved.relationships].map((x) => [x.id, JSON.stringify(x)]));
    const removals = removed.nodes.size + (effective?.relGone.size || 0);
    let edits = 0;
    for (const x of [...work.nodes, ...work.relationships]) {
      if (removed.nodes.has(x.id) || effective?.relGone.has(x.id)) continue;
      if (before.get(x.id) !== JSON.stringify(x)) edits += 1;
    }
    const sig = (p) => `${p.category}|${p.status}`;
    const was = new Map((saved.pii || []).map((p) => [piiKey(p.sheet, p.column), sig(p)]));
    const now = new Map((work.pii || []).map((p) => [piiKey(p.sheet, p.column), sig(p)]));
    let piiChanges = 0;
    for (const k of new Set([...was.keys(), ...now.keys()])) if (was.get(k) !== now.get(k)) piiChanges += 1;
    return { edits, removals, pii: piiChanges };
  }, [work, saved, removed, effective]);

  const toggle = (kind, id, on) => setRemoved((r) => {
    const next = { nodes: new Set(r.nodes), rels: new Set(r.rels) };
    if (on) next[kind].add(id); else next[kind].delete(id);
    return next;
  });

  const setWorkPii = (sheet, column, change) =>
    setWork((w) => ({ ...w, pii: applyPii(w.pii || [], sheet, column, change) }));
  const setDraftPii = (sheet, column, change) =>
    setEditing((e) => ({ ...e, pii: applyPii(e.pii, sheet, column, change) }));
  const startEdit = (kind, item) => setEditing({ kind, id: item.id, draft: clone(item), pii: clone(work.pii || []) });

  const saveEdit = () => {
    const { kind, id, draft, pii: draftPii } = editing;
    setWork((w) => {
      const next = clone(w);
      next.pii = draftPii;
      if (kind === "node") {
        const old = next.nodes.find((n) => n.id === id);
        if (old.label !== draft.label) {
          next.relationships.forEach((r) => {
            if (r.from.label === old.label) r.from.label = draft.label;
            if (r.to.label === old.label) r.to.label = draft.label;
          });
        }
        next.nodes = next.nodes.map((n) => (n.id === id ? draft : n));
      } else {
        next.relationships = next.relationships.map((r) => (r.id === id ? draft : r));
      }
      return next;
    });
    setEditing(null);
  };

  const discard = () => {
    setWork(clone(saved));
    setRemoved({ nodes: new Set(), rels: new Set() });
    setEditing(null);
    setAdding(null);
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api(`/kbs/${kb}/submit`, { method: "POST", json: { schema: effective.schema } });
      navigate(`/kbs/${kb}/jobs/${r.job_id}`);
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };

  const reextract = async () => {
    try {
      const r = await api(`/kbs/${kb}/extract`, { method: "POST" });
      navigate(`/kbs/${kb}/jobs/${r.job_id}`);
    } catch (e) {
      setError(e);
    }
  };

  if (error && !work) return <div className="page"><ErrorBox error={error} /><Link to="/workspace">Back to workspace</Link></div>;
  if (!work) return <div className="page muted">Loading review…</div>;

  const sheets = Object.keys(saved.sheets || {});
  const columnsOf = (sheet) => Object.keys(saved.sheets?.[sheet]?.columns || {});
  const labels = work.nodes.filter((n) => !removed.nodes.has(n.id)).map((n) => n.label);
  const pending = counts.edits + counts.removals + counts.pii;
  const activePii = (work.pii || []).filter(piiActive).length;
  const summary = preview.summary || {};
  const fmt = (n) => (n ?? 0).toLocaleString();

  return (
    <div style={{ display: "flex", flexDirection: "column", flexGrow: 1 }}>
      <div className="page" style={{ paddingBottom: 100 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16, flexWrap: "wrap" }}>
          <div>
            <div className="muted small"><Link to="/workspace">Workspace</Link> / {kb} / Review extraction</div>
            <h1 style={{ marginTop: 4 }}>Review extracted graph</h1>
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <span className="badge b" title="Kinds of things in the graph">{summary.node_types ?? 0} node types</span>
            <span className="badge b" title="Nodes the file will create (distinct keys)">{fmt(summary.entities)} entities</span>
            <span className="badge b" title="Kinds of links between node types">{summary.relationship_types ?? 0} relationship types</span>
            <span className="badge b" title="Links the file will create">{fmt(summary.relationships)} relationships</span>
            <span className="badge b">{activePii} PII columns</span>
            <span className="badge w">{pending} pending changes</span>
          </div>
        </div>
        <ErrorBox error={error} />

        {/* ---------------- node types */}
        <div className="card panel">
          <div className="panel-head">
            <div>
              <div className="section">Node types and entities</div>
              <div className="help">
                A <strong>node type</strong> is a kind of thing in the graph (for example Customer). Every distinct value
                of its key in the source sheet becomes one <strong>entity</strong> (a node) of that type, carrying the
                listed properties. Hover a property to see the column it comes from.
              </div>
            </div>
            <button className="act" onClick={() => setAdding("node")}>+ Add node type</button>
          </div>
          <div className="panel-body">
            <table>
              <thead>
                <tr>
                  <th style={{ width: "16%" }}>Node type (label)</th>
                  <th style={{ width: "13%" }}>Source sheet</th>
                  <th style={{ width: "17%" }}>Unique key (property ← column)</th>
                  <th>Properties stored on each entity</th>
                  <th style={{ width: 90, textAlign: "right" }}>Entities</th>
                  <th style={{ width: 120 }} />
                </tr>
              </thead>
              <tbody>
                {adding === "node" && (
                  <AddNode sheets={sheets} columnsOf={columnsOf} onCancel={() => setAdding(null)}
                           onAdd={(n) => { setWork((w) => ({ ...w, nodes: [...w.nodes, n] })); setAdding(null); }} />
                )}
                {work.nodes.map((n) => {
                  const isRemoved = removed.nodes.has(n.id);
                  const isEditing = editing?.kind === "node" && editing.id === n.id;
                  const d = isEditing ? editing.draft : n;
                  const set = (patch) => setEditing({ ...editing, draft: { ...editing.draft, ...patch } });
                  return (
                    <tr key={n.id} className={isRemoved ? "removed" : isEditing ? "editing" : ""}>
                      <td className="strike" style={{ fontWeight: 600 }}>
                        {isEditing ? <input className="inp sm" value={d.label} aria-label="Label"
                                            onChange={(e) => set({ label: e.target.value })} /> : n.label}
                        {n.role === "embedded" && !isEditing && <div className="muted small">derived from a column</div>}
                      </td>
                      <td className="strike small">{n.sheet}</td>
                      <td className="strike mono small">
                        {isEditing ? (
                          <span style={{ display: "inline-flex", alignItems: "center", flexWrap: "wrap", gap: 2 }}>
                            <input className="inp sm" value={d.key.name} aria-label="Key property"
                                   onChange={(e) => set({ key: { ...d.key, name: e.target.value } })} />
                          </span>
                        ) : <>{n.key.name}{piiActive(pii.get(piiKey(n.sheet, n.key.column))) && <span className="badge pii">PII</span>}
                              <span className="colsrc">← {n.key.column}</span></>}
                      </td>
                      <td className="strike">
                        {isEditing ? <PropEditor props={d.properties} onChange={(properties) => set({ properties })} />
                                   : <PropList props={n.properties} pii={pii} sheet={n.sheet} />}
                      </td>
                      <td style={{ textAlign: "right" }}>{(n.count ?? 0).toLocaleString()}</td>
                      <td>
                        <Actions removed={isRemoved} editing={isEditing}
                                 onEdit={() => startEdit("node", n)}
                                 onDelete={() => toggle("nodes", n.id, true)} onUndo={() => toggle("nodes", n.id, false)}
                                 onSave={saveEdit} onCancel={() => setEditing(null)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* ---------------- relationship types */}
        <div className="card panel">
          <div className="panel-head">
            <div>
              <div className="section">Relationship types</div>
              <div className="help">
                A <strong>relationship type</strong> links two node types in the direction shown (From → To). Every row
                of the source sheet that connects them becomes one <strong>relationship</strong>; optional ones are skipped
                when the row has no value.
              </div>
            </div>
            <button className="act" onClick={() => setAdding("rel")}>+ Add relationship</button>
          </div>
          <div className="panel-body">
            <table>
              <thead>
                <tr>
                  <th style={{ width: "14%" }}>From (node type)</th>
                  <th style={{ width: "17%" }}>Relationship type</th>
                  <th style={{ width: "14%" }}>To (node type)</th>
                  <th style={{ width: "20%" }}>Defined by (sheet: column → column)</th>
                  <th>Properties</th>
                  <th style={{ width: 125, textAlign: "right" }}>Relationships</th>
                  <th style={{ width: 120 }} />
                </tr>
              </thead>
              <tbody>
                {adding === "rel" && (
                  <AddRel sheets={sheets} columnsOf={columnsOf} labels={labels} onCancel={() => setAdding(null)}
                          onAdd={(r) => { setWork((w) => ({ ...w, relationships: [...w.relationships, r] })); setAdding(null); }} />
                )}
                {work.relationships.map((r) => {
                  const isRemoved = effective.relGone.has(r.id);
                  const isEditing = editing?.kind === "rel" && editing.id === r.id;
                  const d = isEditing ? editing.draft : r;
                  const set = (patch) => setEditing({ ...editing, draft: { ...editing.draft, ...patch } });
                  return (
                    <tr key={r.id} className={isRemoved ? "removed" : isEditing ? "editing" : ""}>
                      <td className="strike" style={{ fontWeight: 600 }}>{r.from.label}</td>
                      <td className="strike mono small">
                        {isEditing ? <input className="inp sm" value={d.type} aria-label="Relationship type"
                                            onChange={(e) => set({ type: e.target.value })} /> : r.type}
                        {!isEditing && <div className="muted small">{r.required ? "required" : "optional"}</div>}
                      </td>
                      <td className="strike" style={{ fontWeight: 600 }}>{r.to.label}</td>
                      <td className="strike small">
                        {r.sheet}: <span className="mono">{r.from.column} → {r.to.column}</span>
                      </td>
                      <td className="strike">
                        {isEditing ? <PropEditor props={d.properties} onChange={(properties) => set({ properties })} />
                                   : <PropList props={r.properties} pii={pii} sheet={r.sheet} />}
                      </td>
                      <td style={{ textAlign: "right" }}>{r.count != null ? r.count.toLocaleString() : "—"}</td>
                      <td>
                        <Actions removed={isRemoved} editing={isEditing}
                                 onEdit={() => startEdit("rel", r)}
                                 onDelete={() => toggle("rels", r.id, true)}
                                 onUndo={() => {
                                   toggle("rels", r.id, false);
                                   const n = work.nodes.find((x) => removed.nodes.has(x.id) && [r.from.label, r.to.label].includes(x.label));
                                   if (n) toggle("nodes", n.id, false);
                                 }}
                                 onSave={saveEdit} onCancel={() => setEditing(null)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        <PiiPanel work={work} effective={effective} assessed={assessed} categories={categories} disabled={!!editing}
                  onPii={setWorkPii} />

        {preview.errors.length > 0 && (
          <div className="error" role="alert">
            <strong>Fix before submitting:</strong>
            <ul style={{ margin: "6px 0 0 18px", padding: 0 }}>{preview.errors.map((e) => <li key={e}>{e}</li>)}</ul>
          </div>
        )}

        <CypherPanel title="Generated Cypher (updates as you edit)" code={preview.cypher}
                     meta={`${preview.cypher.length} statements`} />
      </div>

      <div style={{ position: "sticky", bottom: 0, background: "#fff", borderTop: "1px solid var(--line)", padding: "16px 32px",
                    display: "flex", alignItems: "center", gap: 12 }}>
        <div className="muted grow">
          {counts.edits} edit{counts.edits === 1 ? "" : "s"}, {counts.removals} removal{counts.removals === 1 ? "" : "s"} and{" "}
          {counts.pii} PII change{counts.pii === 1 ? "" : "s"} pending.
          Nothing is written to the graph until you submit.
        </div>
        <button className="btn link" onClick={reextract}>Re-run extraction</button>
        <button className="btn sec" onClick={discard} disabled={!pending}>Discard</button>
        <button className="btn" onClick={submit} disabled={busy || preview.errors.length > 0 || !!editing}>
          {busy ? "Submitting…" : "Submit and build graph"}
        </button>
      </div>
    </div>
  );
}

function AddNode({ sheets, columnsOf, onAdd, onCancel }) {
  const [sheet, setSheet] = useState(sheets[0]);
  const [label, setLabel] = useState("");
  const [key, setKey] = useState(columnsOf(sheets[0])[0]);
  const [props, setProps] = useState([]);
  const cols = columnsOf(sheet);
  const snake = (c) => c.toLowerCase().replace(/\(.*?\)/g, "").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  return (
    <tr className="editing">
      <td colSpan={6}>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label>Sheet<select className="inp sm" aria-label="Sheet" value={sheet} onChange={(e) => { setSheet(e.target.value); setKey(columnsOf(e.target.value)[0]); setProps([]); }}>
            {sheets.map((s) => <option key={s}>{s}</option>)}</select></label>
          <label>Label<input className="inp sm" aria-label="New label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Region" /></label>
          <label>Key column<select className="inp sm" aria-label="Key column" value={key} onChange={(e) => setKey(e.target.value)}>
            {cols.map((c) => <option key={c}>{c}</option>)}</select></label>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", margin: "8px 0" }} className="small">
          {cols.filter((c) => c !== key).map((c) => (
            <label key={c}><input type="checkbox" checked={props.includes(c)}
                                  onChange={(e) => setProps(e.target.checked ? [...props, c] : props.filter((x) => x !== c))} /> {c}</label>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn sm" disabled={!label.trim()} onClick={() => onAdd({
            id: `new_n${Date.now()}`, label: label.trim(), sheet, role: "embedded", count: 0,
            key: { name: snake(key) || "key", column: key },
            properties: props.map((c) => ({ name: snake(c) || "value", column: c, type: "string" })),
          })}>Add</button>
          <button className="btn sec sm" onClick={onCancel}>Cancel</button>
        </div>
      </td>
    </tr>
  );
}

function AddRel({ sheets, columnsOf, labels, onAdd, onCancel }) {
  const [sheet, setSheet] = useState(sheets[0]);
  const [from, setFrom] = useState(labels[0]);
  const [to, setTo] = useState(labels[1] || labels[0]);
  const [fromCol, setFromCol] = useState(columnsOf(sheets[0])[0]);
  const [toCol, setToCol] = useState(columnsOf(sheets[0])[1] || columnsOf(sheets[0])[0]);
  const [type, setType] = useState("");
  const cols = columnsOf(sheet);
  const pick = (name, value, set, options) => (
    <select className="inp sm" aria-label={name} value={value} onChange={(e) => set(e.target.value)}>
      {options.map((o) => <option key={o}>{o}</option>)}
    </select>
  );
  return (
    <tr className="editing">
      <td colSpan={7}>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label>Sheet{pick("Sheet", sheet, (s) => { setSheet(s); setFromCol(columnsOf(s)[0]); setToCol(columnsOf(s)[0]); }, sheets)}</label>
          <label>From{pick("From", from, setFrom, labels)}</label>
          <label>via column{pick("From column", fromCol, setFromCol, cols)}</label>
          <label>Type<input className="inp sm" aria-label="Type" value={type} onChange={(e) => setType(e.target.value)} placeholder="e.g. LOCATED_IN" /></label>
          <label>To{pick("To", to, setTo, labels)}</label>
          <label>via column{pick("To column", toCol, setToCol, cols)}</label>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button className="btn sm" disabled={!type.trim()} onClick={() => onAdd({
            id: `new_r${Date.now()}`, type: type.trim(), sheet, required: false, properties: [],
            from: { label: from, column: fromCol }, to: { label: to, column: toCol },
          })}>Add</button>
          <button className="btn sec sm" onClick={onCancel}>Cancel</button>
        </div>
      </td>
    </tr>
  );
}
