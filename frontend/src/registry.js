// Plugin registry — auto-discovers every module under src/modules/<name>/index.jsx.
//
// To add a frontend module: create src/modules/<name>/index.jsx that exports
//   export const meta = { id, title, description, icon, path, order };
//   export default function ModuleView() { ... }
// It appears in the sidebar and routing automatically. No edits here.

const found = import.meta.glob("./modules/*/index.jsx", { eager: true });

export const registry = Object.values(found)
  .filter((m) => m.meta && m.default)
  .map((m) => ({ ...m.meta, Component: m.default }))
  .sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
