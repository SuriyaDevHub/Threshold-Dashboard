import { STATUS_LABEL } from "../lib/format.js";

export function ModuleHeader({ title, description, actions }) {
  return (
    <header className="module-header">
      <div>
        <h1 className="module-title">{title}</h1>
        {description && <p className="module-desc">{description}</p>}
      </div>
      {actions && <div className="module-actions">{actions}</div>}
    </header>
  );
}

export function Card({ title, children, className = "" }) {
  return (
    <section className={`card ${className}`}>
      {title && <h2 className="card-title">{title}</h2>}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, status }) {
  return (
    <div className={`stat ${status ? `stat--${status}` : ""}`}>
      <div className="stat-value mono">{value}</div>
      <div className="stat-label">{label}</div>
      {sub && <div className="stat-sub mono">{sub}</div>}
    </div>
  );
}

export function StatusBadge({ status }) {
  return (
    <span className={`badge badge--${status}`}>{STATUS_LABEL[status] || status}</span>
  );
}

export function Loader({ label = "Loading…" }) {
  return (
    <div className="loader">
      <span className="spinner" /> {label}
    </div>
  );
}

export function EmptyState({ title, hint }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
    </div>
  );
}

export function ErrorState({ error }) {
  return (
    <div className="errorbox">
      <strong>Couldn’t load data.</strong>
      <div className="mono">{String(error?.message || error)}</div>
      <div className="empty-hint">
        Check the backend is running on :8000 and USE_MOCK is set as expected.
      </div>
    </div>
  );
}
