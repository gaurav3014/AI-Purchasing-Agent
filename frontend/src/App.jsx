import { useEffect, useState } from "react";
import "./App.css";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

function DecisionBadge({ decision }) {
  const colors = {
    accept: "#1a7f37",
    modify: "#9a6700",
    reject: "#cf222e",
    investigate: "#8250df",
    accept_shortfall: "#1a7f37",
    additional_po: "#9a6700",
    escalate: "#8250df",
    plan_sufficient: "#1a7f37",
    increase_order: "#9a6700",
  };
  return (
    <span className="badge" style={{ background: colors[decision] || "#57606a" }}>
      {decision?.replaceAll("_", " ").toUpperCase()}
    </span>
  );
}

// Renders any JSON value (object/array/primitive) as nested HTML tables
// instead of a raw pretty-printed blob -- objects become key/value rows,
// arrays of objects become column tables, everything else renders inline.
function JsonValue({ value }) {
  if (value === null || value === undefined) return <em className="json-null">—</em>;
  if (typeof value === "object") return <JsonTable data={value} />;
  return <>{String(value)}</>;
}

function JsonTable({ data }) {
  if (Array.isArray(data)) {
    if (data.length === 0) return <em className="json-null">[]</em>;
    const isObjectRows = data.every(v => v !== null && typeof v === "object" && !Array.isArray(v));
    if (isObjectRows) {
      const columns = Array.from(new Set(data.flatMap(row => Object.keys(row))));
      return (
        <table className="json-table">
          <thead>
            <tr>{columns.map(c => <th key={c}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {data.map((row, i) => (
              <tr key={i}>{columns.map(c => <td key={c}><JsonValue value={row[c]} /></td>)}</tr>
            ))}
          </tbody>
        </table>
      );
    }
    return (
      <ul className="json-array-list">
        {data.map((v, i) => <li key={i}><JsonValue value={v} /></li>)}
      </ul>
    );
  }
  const entries = Object.entries(data ?? {});
  if (entries.length === 0) return <em className="json-null">{"{}"}</em>;
  return (
    <table className="json-table">
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <th>{k}</th>
            <td><JsonValue value={v} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ToolTrace({ trace }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="trace">
      <button className="link-btn" onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Show"} evidence-gathering trace ({trace.length} tool calls)
      </button>
      {open && (
        <div className="trace-list">
          {trace.map((t, i) => (
            <div key={i} className="trace-item">
              <div className="trace-tool">{i + 1}. {t.tool}({JSON.stringify(t.args)})</div>
              <div className="trace-result-wrap">
                <JsonTable data={t.result.result ?? t.result} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Lightweight markdown-lite for LLM reasoning text: turns "1. **Label**: ..."
// style numbered breakdowns into a real <ol>, and **bold** into <strong>,
// instead of dumping one dense paragraph.
function renderInline(text) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) =>
    part.startsWith("**") && part.endsWith("**")
      ? <strong key={i}>{part.slice(2, -2)}</strong>
      : <span key={i}>{part}</span>
  );
}

// LLM reasoning often reads as one dense paragraph with numbered items
// inline ("...requirements: 1. **X**: ... 2. **Y**: ...") rather than on
// separate lines, so detect the numbered markers wherever they fall in the
// text, not just at line starts.
const NUMBERED_MARKER = /(?:^|\s)(\d{1,2})\.\s+(?=[A-Z*])/g;
const CONCLUSION_START = /\.\s+(?=(Since|Therefore|Thus|Given|As a result|Overall|Consequently|In conclusion)\b)/;

function splitNumberedSections(text) {
  const matches = [...text.matchAll(NUMBERED_MARKER)];
  if (matches.length < 2) return null;

  const intro = text.slice(0, matches[0].index).trim();
  const items = matches.map((m, i) => {
    const start = m.index + m[0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    return text.slice(start, end).trim();
  });

  // The concluding sentence after the last item often runs on with no
  // marker of its own -- peel it off into its own paragraph if present.
  let outro = "";
  const last = items[items.length - 1];
  const m = last.match(CONCLUSION_START);
  if (m) {
    items[items.length - 1] = last.slice(0, m.index + 1).trim();
    outro = last.slice(m.index + 1).trim();
  }

  return { intro, items, outro };
}

function FormattedText({ text }) {
  if (!text) return null;
  const trimmed = text.trim();
  const sections = splitNumberedSections(trimmed);

  if (!sections) {
    const paragraphs = trimmed.split(/\n+/).map(l => l.trim()).filter(Boolean);
    return <>{paragraphs.map((l, i) => <p key={i}>{renderInline(l)}</p>)}</>;
  }
  return (
    <>
      {sections.intro && <p>{renderInline(sections.intro)}</p>}
      <ol className="reasoning-list">
        {sections.items.map((l, i) => <li key={i}>{renderInline(l)}</li>)}
      </ol>
      {sections.outro && <p>{renderInline(sections.outro)}</p>}
    </>
  );
}

const SCENARIO_LABELS = {
  recommendation_review: "Recommendation Review",
  supplier_shortfall: "Supplier Cannot Fulfil the Purchase",
  demand_spike: "Demand/Forecast Has Changed",
};

function ScenarioBadge({ scenario }) {
  return (
    <span className="pill scenario-badge">
      {SCENARIO_LABELS[scenario] || scenario}
    </span>
  );
}

function TriggerBadge({ triggeredBy }) {
  const isCron = triggeredBy === "cron";
  return (
    <span className={`pill trigger-badge ${isCron ? "trigger-cron" : "trigger-manual"}`}>
      {isCron ? "Auto (cron)" : "Manual"}
    </span>
  );
}

function ApprovalOutcome({ decision }) {
  if (decision.approval_status !== "approved" && decision.approval_status !== "rejected") return null;
  const approved = decision.approval_status === "approved";
  return (
    <div className={`approval-outcome ${approved ? "approved" : "rejected"}`}>
      <strong>{approved ? "✓ Approved" : "✗ Rejected"}</strong>
      {decision.approved_at && (
        <span className="approval-time"> · {new Date(decision.approved_at + "Z").toLocaleString()}</span>
      )}
      {decision.approval_note && <p className="approval-note">“{decision.approval_note}”</p>}
      <p>
        {approved
          ? decision.executed
            ? "The proposed purchase order was created and the warehouse's budget/storage were updated (see \"Action executed\" above)."
            : "Approved, but there was no purchase order proposal for this decision to execute."
          : "Nothing was ever written to the database — under the Proposal Pattern, a proposal only takes effect once approved."}
      </p>
    </div>
  );
}

function DecisionCard({ decision, onApprove, onReject }) {
  return (
    <div className="card">
      <div className="card-header">
        <ScenarioBadge scenario={decision.scenario} />
        <DecisionBadge decision={decision.decision} />
        <TriggerBadge triggeredBy={decision.triggered_by} />
        <span className="confidence">confidence: {(decision.confidence * 100).toFixed(0)}%</span>
        {decision.requires_human_approval && (
          <span className="pill pending">
            {decision.approval_status === "pending" ? "Needs approval" : decision.approval_status}
          </span>
        )}
      </div>
      <div className="reasoning"><FormattedText text={decision.reasoning} /></div>
      <ul className="factors">
        {decision.factors_considered.map((f, i) => <li key={i}>{f}</li>)}
      </ul>
      {decision.action_result && (
        <div className="action-box">
          <strong>{decision.executed ? "Action executed:" : "Proposed action (pending approval):"}</strong>
          <div className="trace-result-wrap">
            <JsonTable data={decision.action_result} />
          </div>
        </div>
      )}
      {decision.validation_result && (
        <div className="validation-box">
          <strong>Validation:</strong> {decision.validation_passed ? "passed" : "failed"}
          <ul>
            {(decision.validation_result.checks || []).map((c, i) => (
              <li key={i} className={c.passed ? "check-ok" : "check-fail"}>
                {c.check}: {c.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
      <ToolTrace trace={decision.tool_call_trace} />
      {decision.requires_human_approval && decision.approval_status === "pending" && (
        <div className="approval-actions">
          <button className="approve-btn" onClick={() => onApprove(decision.log_id)}>Approve</button>
          <button className="reject-btn" onClick={() => onReject(decision.log_id)}>Reject</button>
        </div>
      )}
      <ApprovalOutcome decision={decision} />
    </div>
  );
}

function useCountdown(nextRunAt) {
  const [secondsLeft, setSecondsLeft] = useState(null);
  useEffect(() => {
    if (!nextRunAt) { setSecondsLeft(null); return; }
    const target = new Date(nextRunAt).getTime();
    const tick = () => setSecondsLeft(Math.max(0, Math.round((target - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [nextRunAt]);
  return secondsLeft;
}

function formatDuration(seconds) {
  if (seconds == null) return "";
  if (seconds < 60) return `${seconds}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${seconds % 60}s`;
}

function timeAgo(isoString) {
  if (!isoString) return null;
  const diff = Math.max(0, Math.round((Date.now() - new Date(isoString).getTime()) / 1000));
  return `${formatDuration(diff)} ago`;
}

function CronStatusPanel({ schedule }) {
  const secondsToNextRun = useCountdown(schedule?.next_run_at);

  if (!schedule) {
    return (
      <aside className="sidebar">
        <div className="cron-card">
          <div className="cron-title">Agent cron</div>
          <p className="empty">Loading status…</p>
        </div>
      </aside>
    );
  }

  const running = schedule.is_running;
  const summary = schedule.last_run_summary || [];

  return (
    <aside className="sidebar">
      <div className="cron-card">
        <div className="cron-title">Agent cron</div>

        <div className="cron-status-row">
          <span className={`cron-dot ${running ? "running" : schedule.enabled ? "idle" : "disabled"}`} />
          <span className="cron-status-text">
            {!schedule.enabled ? "Disabled" : running ? "Running now…" : "Idle — waiting for next run"}
          </span>
        </div>

        {schedule.enabled && (
          <div className="cron-detail">
            <div className="cron-detail-row">
              <span>Interval</span>
              <span>every {formatDuration(schedule.interval_seconds)}</span>
            </div>
            {!running && secondsToNextRun != null && (
              <div className="cron-detail-row">
                <span>Next run</span>
                <span>in {formatDuration(secondsToNextRun)}</span>
              </div>
            )}
            {schedule.last_run_finished_at && (
              <div className="cron-detail-row">
                <span>Last run</span>
                <span>{timeAgo(schedule.last_run_finished_at)}</span>
              </div>
            )}
            {schedule.last_run_reviewed_count != null && (
              <div className="cron-detail-row">
                <span>Flagged</span>
                <span>
                  {schedule.last_run_reviewed_count}
                  {schedule.total_products != null ? ` of ${schedule.total_products} products` : " case(s)"}
                </span>
              </div>
            )}
            {summary.length > 0 && (
              <ul className="cron-summary-list">
                {summary.map((s, i) => (
                  <li key={i}>
                    <strong>{s.sku}</strong>{s.product_name && s.product_name !== s.sku ? ` (${s.product_name})` : ""} — {SCENARIO_LABELS[s.scenario] || s.scenario}: {s.decision.replaceAll("_", " ")}
                    {s.requires_human_approval && <span className="cron-needs-approval"> (needs approval)</span>}
                  </li>
                ))}
              </ul>
            )}
            {schedule.last_run_reviewed_count === 0 && (
              <p className="cron-summary-empty">No products flagged last run.</p>
            )}
            {schedule.last_run_error && (
              <div className="cron-detail-row cron-error">
                <span>Last error</span>
                <span>{schedule.last_run_error}</span>
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

export default function App() {
  const [recommendations, setRecommendations] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [schedule, setSchedule] = useState(null);
  const [shortfalls, setShortfalls] = useState([]);
  const [demandSpikes, setDemandSpikes] = useState([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [search, setSearch] = useState("");

  function refreshRecommendations() {
    fetch(`${API_BASE}/api/scenario1/recommendations`).then(r => r.json()).then(setRecommendations).catch(() => {});
  }

  function refreshShortfalls() {
    fetch(`${API_BASE}/api/scenario2/shortfalls`).then(r => r.json()).then(setShortfalls).catch(() => {});
  }

  function refreshDemandSpikes() {
    fetch(`${API_BASE}/api/scenario3/demand-spikes`).then(r => r.json()).then(setDemandSpikes).catch(() => {});
  }

  function refreshDecisions() {
    fetch(`${API_BASE}/api/decisions`).then(r => r.json()).then(setDecisions).catch(() => {});
  }

  useEffect(() => {
    refreshRecommendations();
    refreshShortfalls();
    refreshDemandSpikes();
    refreshDecisions();
    fetch(`${API_BASE}/api/scenario1/schedule`).then(r => r.json()).then(setSchedule).catch(() => {});
    // The cron (see backend app/scheduler.py) sweeps all three scenarios
    // unattended -- poll so runs it makes show up without a manual refresh.
    const id = setInterval(() => {
      refreshRecommendations();
      refreshShortfalls();
      refreshDemandSpikes();
      refreshDecisions();
      fetch(`${API_BASE}/api/scenario1/schedule`).then(r => r.json()).then(setSchedule).catch(() => {});
    }, 5000);
    return () => clearInterval(id);
  }, []);

  // One unified queue: every pending recommendation, supplier shortfall, and
  // demand spike, tagged by which scenario/endpoint it belongs to, so
  // there's a single picker instead of three duplicated ones.
  const pendingItems = [
    ...recommendations.map(r => ({
      key: `rec-${r.id}`, scenario: "recommendation_review", id: r.id,
      label: `${r.recommended_qty} × ${r.sku} (${r.product_name}) at ${r.warehouse_id}`,
    })),
    ...shortfalls.map(s => ({
      key: `po-${s.po_id}`, scenario: "supplier_shortfall", id: s.po_id,
      label: `${s.po_id} — ${s.sku} (${s.product_name}): confirmed ${s.confirmed_qty} of ${s.ordered_qty} (short ${s.shortfall}) at ${s.warehouse_id}`,
    })),
    ...demandSpikes.map(d => ({
      key: `fc-${d.forecast_id}`, scenario: "demand_spike", id: d.forecast_id,
      label: `${d.sku} (${d.product_name}): actual ${d.actual_qty_to_date} of ${d.forecast_qty} forecast in ${d.days_elapsed}d (${d.implied_daily_rate}/day) at ${d.warehouse_id}`,
    })),
  ];

  // One flat, undifferentiated list in the picker -- it's all just pending
  // data to review, regardless of which scenario it happens to be. The
  // scenario only gets called out afterwards, as a tag on the resulting
  // decision card (see ScenarioBadge below).
  const searchQuery = search.trim().toLowerCase();
  const filteredPendingItems = pendingItems.filter(i => !searchQuery || i.label.toLowerCase().includes(searchQuery));

  useEffect(() => {
    if (!selectedKey && pendingItems.length > 0) setSelectedKey(pendingItems[0].key);
    if (selectedKey && !pendingItems.some(i => i.key === selectedKey)) {
      setSelectedKey(pendingItems[0]?.key || "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recommendations, shortfalls, demandSpikes]);
  const selectedItem = pendingItems.find(i => i.key === selectedKey) || null;

  const SUBMIT_ROUTES = {
    recommendation_review: { path: "scenario1/recommendation-review", bodyKey: "recommendation_id" },
    supplier_shortfall: { path: "scenario2/supplier-shortfall", bodyKey: "po_id" },
    demand_spike: { path: "scenario3/demand-spike", bodyKey: "forecast_id" },
  };

  async function submit() {
    if (!selectedItem) return;
    setLoading(true);
    setError(null);
    try {
      const route = SUBMIT_ROUTES[selectedItem.scenario];
      const res = await fetch(`${API_BASE}/api/${route.path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [route.bodyKey]: selectedItem.id }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || "request failed");
      const data = await res.json();
      setDecisions([data, ...decisions]);
      refreshRecommendations();
      refreshShortfalls();
      refreshDemandSpikes();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleApproval(logId, approve) {
    const res = await fetch(`${API_BASE}/api/approvals/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ log_id: logId, approve, note: "" }),
    });
    const updated = await res.json();
    setDecisions(decisions.map(d => (d.log_id === logId ? updated : d)));
  }

  return (
    <div className="app-layout">
      <CronStatusPanel schedule={schedule} />

      <div className="app">
        <header>
          <h1>AI Purchasing Agent</h1>
          <p className="subtitle">Pending items awaiting agent review</p>
        </header>

        <div className="form-row">
          <label>
            Search by SKU, product, or warehouse
            <input
              type="text" placeholder="e.g. SKU-008, Highland Dark Roast, WH-008…"
              value={search} onChange={e => setSearch(e.target.value)}
            />
          </label>
        </div>

        <div className="form-row">
          <label>
            Pending item
            <select value={selectedKey} onChange={e => setSelectedKey(e.target.value)}>
              {pendingItems.length === 0 && <option value="">Nothing pending</option>}
              {pendingItems.length > 0 && filteredPendingItems.length === 0 && (
                <option value="">No matches for "{search}"</option>
              )}
              {filteredPendingItems.map(item => (
                <option key={item.key} value={item.key}>{item.label}</option>
              ))}
            </select>
          </label>
          <button className="submit-btn" onClick={submit} disabled={loading || !selectedItem}>
            {loading ? "Agent working…" : "Review now"}
          </button>
        </div>
        {error && <div className="error">{error}</div>}

        <div className="decisions">
          {decisions.map(d => (
            <DecisionCard
              key={d.log_id}
              decision={d}
              onApprove={(id) => handleApproval(id, true)}
              onReject={(id) => handleApproval(id, false)}
            />
          ))}
          {decisions.length === 0 && <p className="empty">No decisions yet. Submit a recommendation above.</p>}
        </div>
      </div>
    </div>
  );
}
