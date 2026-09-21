import { useActor, ROLES } from "../RoleContext.jsx";

export default function RoleBar() {
  const { actor, setActor, role, setRole } = useActor();
  return (
    <div className="rd-rolebar">
      <span className="rd-rolebar-label">Acting as</span>
      <input className="rd-rolebar-input" value={actor} onChange={(e) => setActor(e.target.value)} />
      <select className="rd-rolebar-select" value={role} onChange={(e) => setRole(e.target.value)}>
        {ROLES.map((r) => <option key={r} value={r}>{r.replace("_", " ")}</option>)}
      </select>
    </div>
  );
}
