// No login system exists in this app (see every other module) — the Rule
// Designer's approval workflow still needs a "who is doing this, as what
// role" concept (spec §34), so it's modeled as an explicit "acting as"
// selector, persisted per-browser, rather than real auth.
import { createContext, useContext, useEffect, useState } from "react";

const RoleCtx = createContext(null);

const DEFAULT_ACTOR = "suriya.dev";
const DEFAULT_ROLE = "RULE_CREATOR";

export function RoleProvider({ children }) {
  const [actor, setActor] = useState(() => localStorage.getItem("rd_actor") || DEFAULT_ACTOR);
  const [role, setRole] = useState(() => localStorage.getItem("rd_role") || DEFAULT_ROLE);

  useEffect(() => { try { localStorage.setItem("rd_actor", actor); } catch {} }, [actor]);
  useEffect(() => { try { localStorage.setItem("rd_role", role); } catch {} }, [role]);

  return (
    <RoleCtx.Provider value={{ actor, setActor, role, setRole }}>{children}</RoleCtx.Provider>
  );
}

export function useActor() {
  const ctx = useContext(RoleCtx);
  if (!ctx) throw new Error("useActor must be used within RoleProvider");
  return ctx;
}

export const ROLES = ["VIEWER", "RULE_CREATOR", "REVIEWER", "APPROVER", "ADMIN"];
