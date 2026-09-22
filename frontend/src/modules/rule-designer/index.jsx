import { useState } from "react";
// Module-scoped styles — self-contained, see styles.css's own header
// comment. This is the only wiring this module's CSS needs; nothing is
// written into the host app's global stylesheet.
import "./styles.css";
import { ModuleHeader } from "../../components/ui.jsx";
import { RoleProvider } from "./RoleContext.jsx";
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

const TOP_TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "products", label: "Products" },
  { id: "rules", label: "Rules" },
  { id: "data", label: "Datasets & Lookups" },
  { id: "versions", label: "Versions" },
  { id: "audit", label: "Audit" },
];

function RuleDesignerInner() {
  const [tab, setTab] = useState("dashboard");
  const [selectedRule, setSelectedRule] = useState(null);

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
