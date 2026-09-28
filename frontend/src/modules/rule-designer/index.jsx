import { useEffect, useState } from "react";
// Module-scoped styles — self-contained, see styles.css's own header
// comment. This is the only wiring this module's CSS needs; nothing is
// written into the host app's global stylesheet.
import "./styles.css";
import { ModuleHeader } from "../../components/ui.jsx";
import { RoleProvider, useActor } from "./RoleContext.jsx";
import RoleBar from "./components/RoleBar.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Products from "./pages/Products.jsx";
import RuleList from "./pages/RuleList.jsx";
import RuleWorkspace from "./pages/RuleWorkspace.jsx";
import DatasetsLookups from "./pages/DatasetsLookups.jsx";
import Versions from "./pages/Versions.jsx";
import Audit from "./pages/Audit.jsx";

export const meta = {
  id: "rule-designer",
  title: "Rule Designer",
  description: "Author, enrich, test, review, approve and publish business rules.",
  icon: "git-branch",
  path: "/rule-designer",
  order: 5,
};

// Dashboard, Products and Rules are all view-safe for a non-admin — each
// page already gates its own mutating controls behind isAdmin (Products
// exactly mirrors how RuleWorkspace/RuleList do it: create/enable/rename
// buttons hidden, toggles disabled, a "Viewing as User" banner shown), and
// their GET routes carry no role check server-side. Only the genuinely
// admin-only workflows stay tab-gated.
const ALL_TOP_TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "products", label: "Products" },
  { id: "rules", label: "Rules" },
  { id: "data", label: "Datasets & Lookups", adminOnly: true },
  { id: "versions", label: "Versions", adminOnly: true },
  { id: "audit", label: "Audit", adminOnly: true },
];

function RuleDesignerInner() {
  const { isAdmin } = useActor();
  const [tab, setTab] = useState(isAdmin ? "dashboard" : "rules");
  const [selectedRule, setSelectedRule] = useState(null);
  const TOP_TABS = ALL_TOP_TABS.filter((t) => isAdmin || !t.adminOnly);

  useEffect(() => {
    const current = ALL_TOP_TABS.find((t) => t.id === tab);
    if (!isAdmin && current?.adminOnly) setTab("rules");
  }, [isAdmin, tab]);

  function openRule(id) { setSelectedRule(id); setTab("rules"); }

  return (
    <>
      <ModuleHeader
        title="Rule Designer"
        description="Visual builder and free-text/AI builder both compile into the same canonical rule model — enrich, dry-run, review, approve and publish to YAML."
        actions={<RoleBar />}
      />

      {!selectedRule && (
        <div className="tabs" style={{ marginBottom: 18 }}>
          {TOP_TABS.map((t) => (
            <button key={t.id} className={`tab ${tab === t.id ? "active" : ""}`} onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </div>
      )}

      {tab === "dashboard" && !selectedRule && <Dashboard onOpenRule={openRule} />}
      {tab === "products" && !selectedRule && <Products />}
      {tab === "rules" && !selectedRule && <RuleList onOpenRule={openRule} />}
      {tab === "rules" && selectedRule && (
        <RuleWorkspace ruleId={selectedRule} onBack={() => setSelectedRule(null)} onDeleted={() => setSelectedRule(null)} />
      )}
      {tab === "data" && !selectedRule && <DatasetsLookups />}
      {tab === "versions" && !selectedRule && <Versions />}
      {tab === "audit" && !selectedRule && <Audit />}
    </>
  );
}

export default function RuleDesigner() {
  return (
    <RoleProvider>
      <RuleDesignerInner />
    </RoleProvider>
  );
}
