import { Routes, Route, NavLink, Navigate } from "react-router-dom";
import { registry } from "./registry.js";
import Icon from "./components/Icon.jsx";

export default function App() {
  const home = registry.find((m) => m.path === "/") || registry[0];

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">OMRC</div>
          <div className="brand-text">
            <span className="brand-name">TCFC</span>
            <span className="brand-sub">Off-Market Rate Control</span>
          </div>
        </div>

        <nav className="nav">
          {registry.map((m) => (
            <NavLink
              key={m.id}
              to={m.path}
              end={m.path === "/"}
              className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
            >
              <Icon name={m.icon} />
              <span>{m.title}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot mono">
          {registry.length} modules · plugin registry
        </div>
      </aside>

      <main className="workspace">
        <Routes>
          {registry.map((m) => {
            const C = m.Component;
            return <Route key={m.id} path={m.path} element={<C />} />;
          })}
          <Route path="*" element={<Navigate to={home?.path || "/"} replace />} />
        </Routes>
      </main>
    </div>
  );
}
