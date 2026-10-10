// Small building blocks used across pages.
import { titleCase } from "../format.js";

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

export function Card({ title, subtitle, actions, children, className = "" }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p className="muted">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Button({ children, onClick, busy, disabled, kind = "default", title }) {
  return (
    <button type="button" className={`button button-${kind}`} onClick={onClick} disabled={disabled || busy} title={title}>
      {busy ? "Working…" : children}
    </button>
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
