import { useCallback, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background, Controls, MiniMap, Handle, Position, addEdge, applyNodeChanges, applyEdgeChanges, MarkerType,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  Database, Filter, Link2, Calculator, GitBranch, Layers, Wand2, Flag, ShieldCheck, Plus,
} from "lucide-react";
import NodeConfigPanel from "./NodeConfigPanel.jsx";

const NODE_ICONS = {
  input: Database, filter: Filter, lookup: Link2, enrichment: Link2, calculate: Calculator,
  condition: GitBranch, group: Layers, transform: Wand2, outcome: Flag, validation: ShieldCheck,
};
const NODE_COLORS = {
  input: "#5b6775", filter: "#c9821a", lookup: "#0e7c7b", enrichment: "#0e7c7b",
  calculate: "#6a4fb6", condition: "#1f8a4c", group: "#1f8a4c", transform: "#0369a1",
  outcome: "#c0392b", validation: "#8a5a1f",
};

function uid(prefix) { return `${prefix}_${Math.random().toString(16).slice(2, 10)}`; }

function newNode(type, index) {
  const base = {
    id: uid("n"), type, label: type.charAt(0).toUpperCase() + type.slice(1),
    position: { x: 0, y: index * 130 },
  };
  if (type === "filter" || type === "condition" || type === "group") {
    base[type === "filter" ? "filter" : "condition"] = { kind: "group", id: uid("grp"), operator: "AND", children: [] };
  }
  if (type === "lookup" || type === "enrichment") {
    base.lookup = {
      lookup_type: "exact", reference_file_id: "", join_keys: [], range_field: null,
      range_low_column: null, range_high_column: null, date_field: null, date_from_column: null,
      date_to_column: null, fields: [], join_type: "left", missing_strategy: "continue_null",
      default_values: {}, flag_field: null, fallback_reference_file_id: null,
      priority_strategy: "first_match", priority_field: null,
    };
  }
  if (type === "calculate") base.calculate = { output_field: "", expression: "", output_type: "numeric" };
  // Seeded with an explicit Alert=true action — the Outcome tab's Status
  // dropdown shows "Alert" as its default for a node with none yet, but
  // that default must actually be written into `outcomes`, or a rule
  // whose author only touches Reason/Commentary saves with no Status at
  // all (what the UI shows would silently not be what got saved).
  if (type === "outcome") base.outcomes = [{ field: "Alert", value: { type: "static", value: true } }];
  if (type === "transform") base.transform = { field: "", op: "round" };
  if (type === "validation") base.validation = { required_columns: [] };
  return base;
}

export function nodeOutputs(node) {
  // Each entry is {field, type} — type is best-effort ("string" when the
  // real type can't be known client-side, e.g. an enriched column).
  if ((node.type === "lookup" || node.type === "enrichment") && node.lookup) {
    const out = (node.lookup.fields || [])
      .filter((f) => f.output_field)
      .map((f) => ({ field: f.output_field, type: "string" }));
    if (node.lookup.flag_field) out.push({ field: node.lookup.flag_field, type: "boolean" });
    return out;
  }
  if (node.type === "calculate" && node.calculate?.output_field) {
    return [{ field: node.calculate.output_field, type: node.calculate.output_type || "numeric" }];
  }
  if (node.type === "transform" && node.transform?.output_field) {
    return [{ field: node.transform.output_field, type: "string" }];
  }
  return [];
}

// baseFields: [{field, type}]. Returns [{field, type}] available BEFORE nodeId.
export function fieldsBeforeNode(nodes, nodeId, baseFields) {
  const out = [...baseFields];
  for (const n of nodes) {
    if (n.id === nodeId) break;
    out.push(...nodeOutputs(n));
  }
  return out;
}

// Inline (not class-based) so the box, border, shadow and text always
// render correctly the instant the DOM node paints — independent of
// whether/when any external stylesheet (ours or ReactFlow's own) has
// loaded in whatever environment this runs in. Literal color values
// here, not var(--token) references, for the same reason: a CSS custom
// property is only resolvable once its defining stylesheet has applied.
const NODE_BOX_STYLE = {
  display: "flex", alignItems: "center", gap: 8, background: "#ffffff",
  border: "1px solid #e4e8ee", borderLeftWidth: 3, borderLeftStyle: "solid",
  borderRadius: 8, padding: "8px 12px", minWidth: 160,
  boxShadow: "0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06)",
  fontFamily: "inherit", boxSizing: "border-box",
};
const HANDLE_STYLE = {
  width: 8, height: 8, background: "#5b6775", border: "1px solid #ffffff", borderRadius: "50%",
};

function RFNode({ data }) {
  const Icon = NODE_ICONS[data.nodeType] || Database;
  const color = NODE_COLORS[data.nodeType] || "#5b6775";
  return (
    <div className="rd-node" style={{ ...NODE_BOX_STYLE, borderLeftColor: color }}>
      <Handle type="target" position={Position.Top} style={HANDLE_STYLE} />
      <div className="rd-node-icon" style={{ color, flexShrink: 0, display: "flex" }}><Icon size={15} /></div>
      <div>
        <div className="rd-node-type" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.04em", color: "#5b6775" }}>
          {data.nodeType}
        </div>
        <div className="rd-node-label" style={{ fontSize: 12.5, fontWeight: 600, color: "#16202b" }}>{data.label}</div>
      </div>
      <Handle type="source" position={Position.Bottom} style={HANDLE_STYLE} />
    </div>
  );
}

const nodeTypes = { rd: RFNode };

const PALETTE = ["filter", "lookup", "calculate", "condition", "transform", "outcome", "validation"];

export default function WorkflowCanvas({ workflow, onChange, baseFields, meta }) {
  const [selectedId, setSelectedId] = useState(null);

  // React Flow measures each node's DOM size on mount and reports it back
  // via a 'dimensions' node-change; a controlled canvas that doesn't retain
  // that size across renders makes RF re-measure (and re-emit) every render,
  // an infinite loop of harmless-but-endless updates. Dimensions live in a
  // ref (not state) so caching them never itself triggers a re-render —
  // only real edits (position/graph changes) go through `onChange`.
  const dimsRef = useRef({});

  const rfNodes = useMemo(() => workflow.nodes.map((n) => {
    const dims = dimsRef.current[n.id];
    return {
      id: n.id, type: "rd", position: n.position || { x: 0, y: 0 },
      data: { label: n.label || n.type, nodeType: n.type },
      selected: n.id === selectedId,
      ...(dims ? { width: dims.width, height: dims.height } : {}),
    };
  }), [workflow.nodes, selectedId]);

  const rfEdges = useMemo(() => workflow.edges.map((e) => ({
    id: e.id, source: e.source, target: e.target, markerEnd: { type: MarkerType.ArrowClosed },
    style: { stroke: "#98a4b3" },
  })), [workflow.edges]);

  const onNodesChange = useCallback((changes) => {
    for (const c of changes) {
      if (c.type === "dimensions" && c.dimensions) dimsRef.current[c.id] = c.dimensions;
    }
    const positionChanges = changes.filter((c) => c.type === "position" && c.position);
    if (positionChanges.length === 0) return;
    const byId = Object.fromEntries(positionChanges.map((c) => [c.id, c.position]));
    onChange({ ...workflow, nodes: workflow.nodes.map((n) => (n.id in byId ? { ...n, position: byId[n.id] } : n)) });
  }, [workflow, onChange]);

  const onEdgesChange = useCallback((changes) => {
    const next = applyEdgeChanges(changes, rfEdges);
    onChange({ ...workflow, edges: next.map((e) => ({ id: e.id, source: e.source, target: e.target })) });
  }, [rfEdges, workflow, onChange]);

  const onConnect = useCallback((conn) => {
    const next = addEdge({ ...conn, id: uid("edge") }, rfEdges);
    onChange({ ...workflow, edges: next.map((e) => ({ id: e.id, source: e.source, target: e.target })) });
  }, [rfEdges, workflow, onChange]);

  function addPaletteNode(type) {
    const node = newNode(type, workflow.nodes.length);
    const lastId = workflow.nodes[workflow.nodes.length - 1]?.id;
    const nextNodes = [...workflow.nodes, node];
    const nextEdges = lastId ? [...workflow.edges, { id: uid("edge"), source: lastId, target: node.id }] : workflow.edges;
    onChange({ nodes: nextNodes, edges: nextEdges });
    setSelectedId(node.id);
  }

  function deleteNode(id) {
    onChange({
      nodes: workflow.nodes.filter((n) => n.id !== id),
      edges: workflow.edges.filter((e) => e.source !== id && e.target !== id),
    });
    setSelectedId(null);
  }

  function updateNode(next) {
    onChange({ ...workflow, nodes: workflow.nodes.map((n) => (n.id === next.id ? next : n)) });
  }

  const selectedNode = workflow.nodes.find((n) => n.id === selectedId);

  return (
    <div className="rd-canvas-wrap">
      <div className="rd-palette">
        {PALETTE.map((t) => {
          const Icon = NODE_ICONS[t];
          return (
            <button key={t} className="rd-palette-btn" onClick={() => addPaletteNode(t)}>
              <Icon size={14} /> {t} <Plus size={11} />
            </button>
          );
        })}
      </div>
      {/* Inline height alongside the CSS class: ReactFlow measures its
          parent synchronously on mount and logs "parent container needs a
          width and a height" (error#004) if that measurement happens
          before the external stylesheet has applied — an inline style is
          guaranteed to be present from the very first paint, independent
          of stylesheet load order/timing in whatever environment this
          runs in. */}
      <div className="rd-canvas" style={{
        height: 480, width: "100%", border: "1px solid #e4e8ee", borderRadius: 8, overflow: "hidden",
      }}>
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, n) => setSelectedId(n.id)}
          onPaneClick={() => setSelectedId(null)}
          fitView
        >
          <Background gap={16} color="#e4e8ee" />
          <Controls />
          <MiniMap pannable zoomable style={{ background: "#f6f7f9" }} />
        </ReactFlow>
      </div>
      {selectedNode && (
        <NodeConfigPanel
          node={selectedNode}
          fields={fieldsBeforeNode(workflow.nodes, selectedNode.id, baseFields)}
          meta={meta}
          onChange={updateNode}
          onClose={() => setSelectedId(null)}
          onDelete={() => deleteNode(selectedNode.id)}
        />
      )}
    </div>
  );
}
