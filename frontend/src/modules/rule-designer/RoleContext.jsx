// This module's own login-free "acting as" selector, until it sits behind
// the real Auth module (dep.py/user.py) — every mutating API call already
// takes a plain {actor, role} object, so swapping this context for one
// backed by the real session is a contained change, not a rewrite.
import { createContext, useContext, useEffect, useState } from "react";

const RoleCtx = createContext(null);

const DEFAULT_ACTOR = "suriya.dev";
const DEFAULT_ROLE = "USER";

export function RoleProvider({ children }) {
  const [actor, setActor] = useState(() => localStorage.getItem("rd_actor") || DEFAULT_ACTOR);
  const [role, setRole] = useState(() => {
    const stored = localStorage.getItem("rd_role");
    return ROLES.includes(stored) ? stored : DEFAULT_ROLE;
  });

  useEffect(() => { try { localStorage.setItem("rd_actor", actor); } catch {} }, [actor]);
  useEffect(() => { try { localStorage.setItem("rd_role", role); } catch {} }, [role]);

  return (
    <RoleCtx.Provider value={{ actor, setActor, role, setRole, isAdmin: role === "ADMIN" }}>
      {children}
    </RoleCtx.Provider>
  );
}

export function useActor() {
  const ctx = useContext(RoleCtx);
  if (!ctx) throw new Error("useActor must be used within RoleProvider");
  return ctx;
}

// Mirrors the production login's two account roles — Admin (create, edit,
// test, submit, approve, publish, enable/disable a rule or a product) and
// User (view published configs, dry-run/shadow-test rules still in draft).
export const ROLES = ["ADMIN", "USER"];
