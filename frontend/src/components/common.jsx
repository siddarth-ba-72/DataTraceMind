// Small building blocks used across pages, in the console and in the lab.
import { titleCase } from "../format.js";
import { Icon } from "./icons.jsx";

// A status is never shown by colour alone: every badge pairs a glyph with a label.
const TONE = {
  good: { glyph: "✓", label: "good" },
  warning: { glyph: "!", label: "warning" },
  serious: { glyph: "▲", label: "serious" },
  critical: { glyph: "✕", label: "critical" },
  neutral: { glyph: "•", label: "" },
  active: { glyph: "◌", label: "" },
};

const STATUS_TONE = {
  // incident lifecycle
  detected: "serious",
  investigating: "active",
  diagnosed: "active",
  repair_proposed: "active",
  validating: "active",
  validation_failed: "critical",
  awaiting_approval: "warning",
  applying: "active",
  application_failed: "critical",
  verifying: "active",
  resolved: "good",
  rejected: "neutral",
  escalated: "serious",
  // severities
  critical: "critical",
  high: "serious",
  medium: "warning",
  low: "neutral",
  // pipeline and events
  SUCCESS: "good",
  FAILED: "critical",
  RUNNING: "active",
  completed: "good",
  failed: "critical",
  running: "active",
  // proposals
  proposed: "active",
  validated: "good",
  approved: "good",
  applied: "good",
  verified: "good",
  superseded: "neutral",
  // trust
  trustworthy: "good",
  under_investigation: "serious",
  stale: "warning",
  untrusted: "critical",
};

export const TRUST_TEXT = {
  trustworthy: "Data is trustworthy",
  under_investigation: "Under investigation",
  stale: "Data is stale",
  untrusted: "Data does not reconcile",
};

export function Badge({ value, tone, children, title }) {
  const resolved = tone || STATUS_TONE[value] || "neutral";
  return (
    <span className={`badge tone-${resolved}`} title={title}>
      <span className="badge-glyph" aria-hidden="true">
        {TONE[resolved].glyph}
      </span>
      {children || titleCase(value)}
    </span>
  );
}

// `flush` lets a table inside run to the card's edges.
export function Card({ id, title, subtitle, actions, children, className = "", flush = false }) {
  return (
    <section id={id} className={`card ${flush ? "card-flush" : ""} ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p className="muted">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function PageHead({ title, subtitle, actions }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Button({ children, onClick, busy, disabled, kind = "default", size, icon, title }) {
  return (
    <button
      type="button"
      className={`button button-${kind} ${size ? `button-${size}` : ""}`}
      onClick={onClick}
      disabled={disabled || busy}
      title={title}
    >
      {icon && !busy && <Icon name={icon} size={size === "large" ? 16 : 14} />}
      {busy ? "Working…" : children}
    </button>
  );
}

// One choice out of a few, shown as a row of buttons.
export function Segmented({ value, options, onChange, label, disabled }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          disabled={disabled}
          title={option.title}
          onClick={() => onChange(option.value)}
        >
          {option.label}
          {option.count !== undefined && <span className="count">{option.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function ErrorNote({ message, onDismiss }) {
  if (!message) return null;
  return (
    <div className="note note-error" role="alert">
      <span>{message}</span>
      {onDismiss && (
        <button type="button" className="link" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </div>
  );
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

export function Json({ value }) {
  return <pre className="json">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}

export function KeyValue({ rows }) {
  return (
    <dl className="kv">
      {rows.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

// Label and value pairs for a narrow side panel.
export function Facts({ rows }) {
  return (
    <dl className="facts">
      {rows
        .filter(([, value]) => value !== null && value !== undefined && value !== "")
        .flatMap(([key, value]) => [<dt key={`${key}-k`}>{key}</dt>, <dd key={`${key}-v`}>{value}</dd>])}
    </dl>
  );
}

// Demo access code, kept in this browser. Rendered only when the server asks for one.
export function AccessCode({ value, onChange }) {
  return (
    <input
      className="access-code"
      type="password"
      placeholder="Demo access code"
      aria-label="Demo access code"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}
