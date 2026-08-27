/* Workflow builder — a real editor over the bot's saved journey.

   Nodes and edges live in local editable state (loaded from the server
   document); the palette adds nodes, nodes drag to move, the inspector edits
   each node's label + runtime configuration and manages its connections.
   Save PUTs the document; the backend validates it structurally, stores
   server-computed issues, and the SAME saved graph is what the runtime
   workflow engine executes on live calls and in the Testing tab. */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { NodeKind, VoiceBot, Workflow, WorkflowEdge, WorkflowNode } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { getWorkflow, saveWorkflow } from "@/services/api";
import type { ApiRequestError } from "@/services/http";
import {
  Button, Callout, CardSkeleton, ConfirmModal, EmptyState, ErrorState, MenuButton, Modal, StatusChip,
} from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const nodeMeta: Record<NodeKind, { icon: IconName; color: string; label: string; desc: string }> = {
  start: { icon: "play", color: "var(--series-2)", label: "Start", desc: "Where every call enters the flow" },
  message: { icon: "message", color: "var(--series-1)", label: "Message", desc: "Bot speaks, then continues" },
  ask: { icon: "help", color: "var(--series-6)", label: "Ask (collect)", desc: "Ask a question, save the answer" },
  intent: { icon: "target", color: "var(--series-4)", label: "Intent router", desc: "Branch on what the caller says" },
  condition: { icon: "workflow", color: "var(--series-3)", label: "Condition", desc: "Branch on a collected variable" },
  api: { icon: "zap", color: "var(--series-8)", label: "API call", desc: "Run a configured API action" },
  knowledge: { icon: "book", color: "var(--series-5)", label: "Knowledge answer", desc: "Answer from the knowledge base" },
  handover: { icon: "headphones", color: "var(--series-7)", label: "Human handover", desc: "Transfer to a human agent" },
  end: { icon: "check-circle", color: "var(--ink-3)", label: "End", desc: "Finish the call" },
};

/* Node kinds whose authored text is spoken — these support delivery modes
   (handover is excluded: the runtime always speaks it as authored). */
const SPEAKING_KINDS: NodeKind[] = ["message", "end", "ask", "intent", "knowledge"];

const NODE_W = 178;
const NODE_H = 54;
const ZOOM_MIN = 0.4;
const ZOOM_MAX = 1.5;

type Issue = { nodeId: string; level: "warning" | "error"; message: string };

const newId = (prefix: string) => `${prefix}_${Math.random().toString(36).slice(2, 8)}`;

/** Mirror of the backend's route-string form for a workflow name
    ("Payment plan journey" → payment_plan_journey). */
export function slugifyWorkflowName(name: string): string {
  return (name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "");
}

/** Canvas display form of an edge label: the runtime treats labels as match
    tokens split on , / | — show the first token and how many more there are. */
export function edgeLabelSummary(label?: string): string {
  if (!label) return "";
  const tokens = label.split(/[|,/]/).map((t) => t.trim()).filter(Boolean);
  if (tokens.length === 0) return "";
  const first = tokens[0].length > 18 ? `${tokens[0].slice(0, 17)}…` : tokens[0];
  return tokens.length > 1 ? `${first} +${tokens.length - 1}` : first;
}

/** One-line config preview shown under the node title on the canvas. */
function nodePreview(n: WorkflowNode): string {
  const c = (n.config ?? {}) as Record<string, unknown>;
  const s = (k: string) => (typeof c[k] === "string" ? (c[k] as string) : "");
  switch (n.kind) {
    case "message": case "end": case "handover": return s("text");
    case "ask": return s("question") || (s("variable") ? `→ ${s("variable")}` : "");
    case "intent": return s("prompt");
    case "condition": return s("variable") ? `${s("variable")} ${s("operator") || "exists"} ${s("value")}`.trim() : "";
    case "knowledge": return s("query") || "Answers from the knowledge base";
    case "api": return s("name");
    default: return n.sub ?? "";
  }
}

/** Client-side mirror of the backend's structural validation (the backend
    remains authoritative — it recomputes issues on every save). */
export function validateGraph(nodes: WorkflowNode[], edges: WorkflowEdge[]): Issue[] {
  const issues: Issue[] = [];
  const start = nodes.find((n) => n.kind === "start");
  const out = new Map<string, WorkflowEdge[]>();
  for (const e of edges) {
    out.set(e.from, [...(out.get(e.from) ?? []), e]);
  }
  const reachable = new Set<string>();
  const stack = start ? [start.id] : [];
  while (stack.length) {
    const cur = stack.pop()!;
    if (reachable.has(cur)) continue;
    reachable.add(cur);
    for (const e of out.get(cur) ?? []) stack.push(e.to);
  }
  for (const n of nodes) {
    const outgoing = out.get(n.id) ?? [];
    const config = n.config ?? {};
    if (start && !reachable.has(n.id)) {
      issues.push({ nodeId: n.id, level: "warning", message: "Not connected to the start node — this step never runs." });
      continue;
    }
    if (n.kind === "condition") {
      if (!config.variable) issues.push({ nodeId: n.id, level: "error", message: "Condition needs a variable to evaluate." });
      if (outgoing.length < 2) issues.push({ nodeId: n.id, level: "warning", message: "Condition should have true and false branches." });
    }
    if (n.kind === "ask" && !config.variable) {
      issues.push({ nodeId: n.id, level: "warning", message: "No variable name set — the node id will be used." });
    }
    if (n.kind === "intent" && outgoing.length === 0) {
      issues.push({ nodeId: n.id, level: "error", message: "Intent node needs at least one outgoing branch." });
    }
    if (!["end", "handover", "start"].includes(n.kind) && outgoing.length === 0) {
      issues.push({ nodeId: n.id, level: "warning", message: "Dead end — no outgoing connection." });
    }
  }
  if (!start && nodes.length) {
    issues.push({ nodeId: nodes[0].id, level: "error", message: "A start node is required." });
  }
  if (start && nodes.length && !nodes.some((n) => ["end", "handover"].includes(n.kind) && reachable.has(n.id))) {
    issues.push({ nodeId: start.id, level: "warning", message: "No end or handover step is reachable — the flow cannot finish." });
  }
  return issues;
}

/** Parse an imported workflow document (from Export JSON). Throws with a
    user-readable message when the shape can't be executed. */
export function parseImportedDefinition(raw: string): { nodes: WorkflowNode[]; edges: WorkflowEdge[] } {
  let doc: unknown;
  try {
    doc = JSON.parse(raw);
  } catch {
    throw new Error("That file is not valid JSON.");
  }
  const d = doc as { nodes?: unknown; edges?: unknown };
  if (!Array.isArray(d.nodes) || !Array.isArray(d.edges)) {
    throw new Error("Expected a workflow export with \"nodes\" and \"edges\" arrays.");
  }
  const kinds = Object.keys(nodeMeta);
  const nodes: WorkflowNode[] = d.nodes.map((n, i) => {
    const o = (n ?? {}) as Record<string, unknown>;
    if (typeof o.id !== "string" || !o.id) throw new Error(`Node ${i + 1} is missing an id.`);
    if (typeof o.kind !== "string" || !kinds.includes(o.kind)) {
      throw new Error(`Node "${o.id}" has an unknown kind "${String(o.kind)}".`);
    }
    return {
      id: o.id,
      kind: o.kind as NodeKind,
      label: typeof o.label === "string" && o.label ? o.label : nodeMeta[o.kind as NodeKind].label,
      x: typeof o.x === "number" ? o.x : 60 + (i % 3) * 220,
      y: typeof o.y === "number" ? o.y : 60 + Math.floor(i / 3) * 110,
      config: typeof o.config === "object" && o.config !== null ? (o.config as Record<string, unknown>) : {},
      ...(typeof o.sub === "string" ? { sub: o.sub } : {}),
    };
  });
  const ids = new Set(nodes.map((n) => n.id));
  const edges: WorkflowEdge[] = d.edges.map((e, i) => {
    const o = (e ?? {}) as Record<string, unknown>;
    if (typeof o.from !== "string" || typeof o.to !== "string" || !ids.has(o.from) || !ids.has(o.to)) {
      throw new Error(`Connection ${i + 1} references a node that doesn't exist.`);
    }
    return {
      id: typeof o.id === "string" && o.id ? o.id : newId("e"),
      from: o.from,
      to: o.to,
      ...(typeof o.label === "string" && o.label ? { label: o.label } : {}),
    };
  });
  return { nodes, edges };
}

export default function WorkflowsTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync<Workflow | null>(async () => {
    try {
      return await getWorkflow(bot.id);
    } catch (e) {
      // No workflow yet is a normal state (the create panel), not an error.
      if ((e as ApiRequestError).status === 404) return null;
      throw e;
    }
  }, [bot.id]);
  const { toast, hasPermission } = useApp();
  const canEdit = hasPermission("manage_workflows") || hasPermission("bots.manage");

  const [nodes, setNodes] = useState<WorkflowNode[]>([]);
  const [edges, setEdges] = useState<WorkflowEdge[]>([]);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [connectFrom, setConnectFrom] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [past, setPast] = useState<{ nodes: WorkflowNode[]; edges: WorkflowEdge[] }[]>([]);
  const [future, setFuture] = useState<{ nodes: WorkflowNode[]; edges: WorkflowEdge[] }[]>([]);
  const [zoom, setZoom] = useState(1);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [statusConfirm, setStatusConfirm] = useState<"approved" | "draft" | null>(null);
  const [discardConfirm, setDiscardConfirm] = useState(false);
  const [pendingImport, setPendingImport] = useState<{ nodes: WorkflowNode[]; edges: WorkflowEdge[] } | null>(null);
  const [mustIncludeDraft, setMustIncludeDraft] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  /* The inspector is a draggable floating popup; its position persists for
     the session and is clamped so the drag handle always stays reachable. */
  const [popupPos, setPopupPos] = useState<{ x: number; y: number } | null>(null);
  const popupDrag = useRef<{ dx: number; dy: number } | null>(null);

  /* Load the server document into the editable state. Selection survives a
     reload (e.g. after save) when the node still exists. */
  useEffect(() => {
    if (q.data) {
      const doc = q.data;
      setNodes(doc.nodes);
      setEdges(doc.edges);
      setIssues(doc.issues as Issue[]);
      setPast([]);
      setFuture([]);
      setDirty(false);
      setSelected((prev) => (prev && doc.nodes.some((n) => n.id === prev) ? prev : null));
      setSelectedEdgeId(null);
      setConnectFrom(null);
    }
  }, [q.data]);

  /* Unsaved edits should survive an accidental refresh/close. */
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  /* Drag-to-move: pointer capture on the node, position updates on move. */
  const dragRef = useRef<{ id: string; dx: number; dy: number; zoom: number; moved: boolean } | null>(null);

  const snapshot = () => {
    setPast((p) => [...p.slice(-49), { nodes, edges }]);
    setFuture([]);
    setDirty(true);
  };

  const undo = () => {
    setPast((p) => {
      if (!p.length) return p;
      const prev = p[p.length - 1];
      setFuture((f) => [...f, { nodes, edges }]);
      setNodes(prev.nodes);
      setEdges(prev.edges);
      setDirty(true);
      return p.slice(0, -1);
    });
  };

  const redo = () => {
    setFuture((f) => {
      if (!f.length) return f;
      const next = f[f.length - 1];
      setPast((p) => [...p, { nodes, edges }]);
      setNodes(next.nodes);
      setEdges(next.edges);
      setDirty(true);
      return f.slice(0, -1);
    });
  };

  const wf = q.data;
  const selectedNode = useMemo(() => nodes.find((n) => n.id === selected) ?? null, [nodes, selected]);
  const selectedEdge = useMemo(() => edges.find((e) => e.id === selectedEdgeId) ?? null, [edges, selectedEdgeId]);
  const selectedIssues = issues.filter((i) => i.nodeId === selected);
  const outgoing = useMemo(() => edges.filter((e) => e.from === selected), [edges, selected]);
  const incoming = useMemo(() => edges.filter((e) => e.to === selected), [edges, selected]);

  /* Reset per-node input drafts when the selection changes. */
  useEffect(() => setMustIncludeDraft(null), [selected]);

  const selectNode = (id: string | null) => {
    setSelected(id);
    setSelectedEdgeId(null);
  };
  const selectEdge = (id: string) => {
    setSelectedEdgeId(id);
    setSelected(null);
    setConnectFrom(null);
  };

  const updateNode = (id: string, patch: Partial<WorkflowNode>) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, ...patch } : n)));
    setDirty(true);
  };
  const updateConfig = (id: string, key: string, value: unknown) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, config: { ...(n.config ?? {}), [key]: value } } : n)));
    setDirty(true);
  };

  const addNode = (kind: NodeKind) => {
    snapshot();
    const id = newId("n");
    const index = nodes.length;
    const node: WorkflowNode = {
      id, kind, label: nodeMeta[kind].label,
      x: 60 + (index % 3) * 220, y: 60 + Math.floor(index / 3) * 110,
      config: {},
    };
    setNodes((ns) => [...ns, node]);
    selectNode(id);
    toast(`${nodeMeta[kind].label} node added — connect it from another node`);
  };

  const duplicateNode = (id: string) => {
    const src = nodes.find((n) => n.id === id);
    if (!src || src.kind === "start") return;
    snapshot();
    const copy: WorkflowNode = {
      ...structuredClone(src),
      id: newId("n"),
      label: `${src.label} copy`,
      x: src.x + 32,
      y: src.y + 32,
    };
    setNodes((ns) => [...ns, copy]);
    selectNode(copy.id);
    toast("Node duplicated — connections were not copied");
  };

  const deleteNode = (id: string) => {
    snapshot();
    setNodes((ns) => ns.filter((n) => n.id !== id));
    setEdges((es) => es.filter((e) => e.from !== id && e.to !== id));
    selectNode(null);
    toast("Node and its connections removed");
  };

  const addEdge = (from: string, to: string) => {
    if (from === to) return;
    if (edges.some((e) => e.from === from && e.to === to)) {
      toast("These nodes are already connected", "error");
      return;
    }
    snapshot();
    setEdges((es) => [...es, { id: newId("e"), from, to }]);
  };

  const removeEdge = (id: string) => {
    snapshot();
    setEdges((es) => es.filter((e) => e.id !== id));
    if (selectedEdgeId === id) setSelectedEdgeId(null);
  };

  /* Branch order matters at runtime (the engine tries a node's outgoing
     edges in document order), so the inspector can reorder them. */
  const moveEdge = (edgeId: string, dir: -1 | 1) => {
    const idx = edges.findIndex((e) => e.id === edgeId);
    if (idx < 0) return;
    const siblings = edges
      .map((e, i) => ({ e, i }))
      .filter((x) => x.e.from === edges[idx].from);
    const pos = siblings.findIndex((x) => x.e.id === edgeId);
    const target = siblings[pos + dir];
    if (!target) return;
    snapshot();
    setEdges((es) => {
      const copy = [...es];
      [copy[idx], copy[target.i]] = [copy[target.i], copy[idx]];
      return copy;
    });
  };

  const autoLayout = () => {
    snapshot();
    const out = new Map<string, string[]>();
    for (const e of edges) out.set(e.from, [...(out.get(e.from) ?? []), e.to]);
    const start = nodes.find((n) => n.kind === "start") ?? nodes[0];
    const depth = new Map<string, number>();
    const queue = start ? [{ id: start.id, d: 0 }] : [];
    while (queue.length) {
      const { id, d } = queue.shift()!;
      if (depth.has(id)) continue;
      depth.set(id, d);
      for (const to of out.get(id) ?? []) queue.push({ id: to, d: d + 1 });
    }
    const rows = new Map<number, number>();
    setNodes((ns) => ns.map((n) => {
      const d = depth.get(n.id) ?? 0;
      const row = rows.get(d) ?? 0;
      rows.set(d, row + 1);
      return { ...n, x: 40 + d * 230, y: 40 + row * 110 };
    }));
    toast("Auto-layout applied — nodes arranged by flow depth");
  };

  const runValidate = () => {
    const found = validateGraph(nodes, edges);
    setIssues(found);
    toast(found.length ? `Validation found ${found.length} issue${found.length === 1 ? "" : "s"} — see panel below` : "Validation clean");
  };

  const persist = async (opts: { status?: "draft" | "approved"; name?: string } = {}) => {
    if (busy) return;
    setBusy(true);
    try {
      const saved = await saveWorkflow(bot.id, {
        nodes, edges,
        ...(opts.status ? { status: opts.status } : {}),
        ...(opts.name ? { name: opts.name } : {}),
      });
      setIssues(saved.issues as Issue[]);
      setDirty(false);
      toast(opts.status === "approved"
        ? `Workflow v${saved.version} activated — live calls pick it up within ~30s`
        : opts.status === "draft"
          ? `Workflow v${saved.version} deactivated (draft)`
          : opts.name
            ? `Workflow renamed and saved as v${saved.version}`
            : `Workflow saved as v${saved.version} ${saved.status}`);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not save workflow", "error");
    } finally {
      setBusy(false);
    }
  };

  const createWorkflow = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await saveWorkflow(bot.id, {
        nodes: [{ id: newId("n"), kind: "start", label: "Call starts", x: 60, y: 60, config: {} }],
        edges: [],
      });
      toast("Workflow created — add steps from the palette");
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not create the workflow", "error");
    } finally {
      setBusy(false);
    }
  };

  const exportJson = () => {
    if (!wf) return;
    const doc = { name: wf.name, exportedFrom: { botId: bot.id, workflowId: wf.id, version: wf.version }, nodes, edges };
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${slugifyWorkflowName(wf.name) || "workflow"}.workflow.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast("Workflow JSON downloaded");
  };

  const onImportFile: React.ChangeEventHandler<HTMLInputElement> = async (ev) => {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    try {
      const parsed = parseImportedDefinition(await file.text());
      if (nodes.length > 0) setPendingImport(parsed);
      else applyImport(parsed);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not read that file", "error");
    }
  };

  const applyImport = (parsed: { nodes: WorkflowNode[]; edges: WorkflowEdge[] }) => {
    snapshot();
    setNodes(parsed.nodes);
    setEdges(parsed.edges);
    setIssues(validateGraph(parsed.nodes, parsed.edges));
    selectNode(null);
    setPendingImport(null);
    toast(`Imported ${parsed.nodes.length} nodes — review, then Save version`);
  };

  /* Keyboard shortcuts (skipped while typing in a field). */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) return;
      if (renameOpen || statusConfirm !== null || discardConfirm || pendingImport !== null) return;
      if (e.key === "Escape") {
        if (connectFrom) setConnectFrom(null);
        else { setSelected(null); setSelectedEdgeId(null); }
        return;
      }
      if (!canEdit) return;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && !e.shiftKey && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
      if ((mod && e.shiftKey && e.key.toLowerCase() === "z") || (mod && e.key.toLowerCase() === "y")) { e.preventDefault(); redo(); return; }
      if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); if (dirty && !busy) void persist(); return; }
      if (e.key === "Delete" || e.key === "Backspace") {
        if (selectedEdgeId) { removeEdge(selectedEdgeId); return; }
        if (selected) {
          const n = nodes.find((x) => x.id === selected);
          if (n && n.kind !== "start") deleteNode(selected);
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading) return <CardSkeleton rows={10} />;

  /* No workflow yet — a normal state with a create path, not an error. */
  if (!wf) {
    return (
      <div className="card">
        <EmptyState
          icon="workflow"
          title="No workflow yet"
          body={canEdit
            ? "Design this bot's conversation journey on a visual canvas — messages, questions, branches, API calls and handovers. The saved flow is what runs on live calls and in Testing."
            : "This bot has no conversation journey yet. Someone with workflow permissions can create one."}
          action={canEdit
            ? <Button variant="primary" icon="plus" busy={busy} onClick={() => void createWorkflow()}>Create workflow</Button>
            : undefined}
        />
      </div>
    );
  }

  /* Parallel edges (same from → to, e.g. a refuse-tokens branch AND an else
     branch to the next rung) would overlap exactly — offset each duplicate so
     both stay visible and clickable. */
  const parallelIndex = new Map<string, number>();
  {
    const seen = new Map<string, number>();
    for (const e of edges) {
      const key = `${e.from}→${e.to}`;
      const n = seen.get(key) ?? 0;
      parallelIndex.set(e.id, n);
      seen.set(key, n + 1);
    }
  }

  const edgePath = (from: WorkflowNode, to: WorkflowNode, off = 0) => {
    const x1 = from.x + NODE_W;
    const y1 = from.y + NODE_H / 2 + off;
    const x2 = to.x;
    const y2 = to.y + NODE_H / 2 + off;
    if (to.x < from.x + NODE_W && Math.abs(to.y - from.y) > NODE_H) {
      const vx1 = from.x + NODE_W / 2 + off;
      const vy1 = from.y + NODE_H;
      const vx2 = to.x + NODE_W / 2 + off;
      const vy2 = to.y;
      return { d: `M${vx1} ${vy1} C ${vx1} ${vy1 + 34}, ${vx2} ${vy2 - 34}, ${vx2} ${vy2}`, lx: (vx1 + vx2) / 2, ly: (vy1 + vy2) / 2 + off };
    }
    const mx = (x1 + x2) / 2;
    return { d: `M${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`, lx: mx, ly: (y1 + y2) / 2 - 6 };
  };

  const clampPopup = (x: number, y: number) => ({
    x: Math.min(Math.max(8, x), Math.max(8, window.innerWidth - 140)),
    y: Math.min(Math.max(8, y), Math.max(8, window.innerHeight - 56)),
  });
  /* Until the user drags it, the popup docks to the canvas's top-right corner
     (never over the toolbar above); afterwards it stays where they put it. */
  const popupPosition = popupPos ?? (() => {
    const width = Math.min(380, window.innerWidth - 24);
    const rect = scrollRef.current?.getBoundingClientRect();
    if (rect && rect.width > 0) return clampPopup(rect.right - width - 18, Math.max(rect.top + 14, 64));
    return clampPopup(window.innerWidth - width - 36, 200);
  })();

  const canvasW = Math.max(660, ...nodes.map((n) => n.x + NODE_W + 60));
  const canvasH = Math.max(570, ...nodes.map((n) => n.y + NODE_H + 60));
  const configText = (key: string): string => String((selectedNode?.config ?? {})[key] ?? "");
  const mustIncludeValue = mustIncludeDraft
    ?? ((selectedNode?.config?.responseMustInclude as string[] | undefined) ?? []).join(", ");

  const setZoomClamped = (z: number) => setZoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(z * 20) / 20)));
  const zoomToFit = () => {
    const el = scrollRef.current;
    if (!el) return;
    setZoomClamped(Math.min(1, (el.clientWidth - 24) / canvasW, (el.clientHeight - 24) / canvasH));
    el.scrollTo({ left: 0, top: 0 });
  };

  /* When something is selected, its edges stay prominent and the rest fade.
     In dense graphs, idle labels are suppressed entirely — select a node or a
     connection to reveal the ones that matter. */
  const hasSelection = Boolean(selected || selectedEdgeId);
  const showIdleLabels = edges.length <= 24;
  const edgeIsActive = (e: WorkflowEdge) =>
    e.id === selectedEdgeId || (selected !== null && (e.from === selected || e.to === selected));

  const errorCount = issues.filter((i) => i.level === "error").length;
  const warningCount = issues.length - errorCount;
  const startExists = nodes.some((n) => n.kind === "start");

  const menuActions = [
    ...(canEdit ? [{ label: "Rename workflow", icon: "edit" as IconName, onClick: () => { setRenameValue(wf.name); setRenameOpen(true); } }] : []),
    { label: "Export JSON", icon: "download" as IconName, onClick: exportJson },
    ...(canEdit ? [{ label: "Import JSON…", icon: "upload" as IconName, onClick: () => fileRef.current?.click() }] : []),
  ];

  return (
    <div className="col gap-16">
      <input ref={fileRef} type="file" accept=".json,application/json" hidden aria-hidden="true" onChange={onImportFile} />

      <div className="row-between wrap" style={{ gap: 10 }}>
        <div className="col gap-4" style={{ minWidth: 0 }}>
          <div className="row gap-8 wrap">
            <span className="t-strong" style={{ fontSize: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 360 }} title={wf.name}>
              {wf.name}
            </span>
            {canEdit && (
              <button className="btn-icon" style={{ width: 24, height: 24 }} title="Rename workflow" aria-label="Rename workflow"
                onClick={() => { setRenameValue(wf.name); setRenameOpen(true); }}>
                <Icon name="edit" size={13} />
              </button>
            )}
            <code className="tag">v{wf.version}</code>
            <StatusChip status={wf.status} label={wf.status === "approved" ? "Active" : undefined} />
            {dirty && <span className="chip chip-warning" title="You have unsaved edits"><Icon name="clock" size={11} />Unsaved changes</span>}
            {!canEdit && <span className="chip chip-neutral" title="You don't have permission to edit workflows"><Icon name="lock" size={11} />View only</span>}
          </div>
          <span className="t-micro">
            {nodes.length} {nodes.length === 1 ? "step" : "steps"} · {edges.length} {edges.length === 1 ? "connection" : "connections"} · edited by {wf.updatedBy} · {new Date(wf.updatedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
          </span>
        </div>
        <div className="wf-toolbar">
          {canEdit && (
            <>
              <Button size="sm" icon="undo" disabled={past.length === 0} onClick={undo} title="Undo (Ctrl+Z)">Undo</Button>
              <Button size="sm" icon="redo" disabled={future.length === 0} onClick={redo} title="Redo (Ctrl+Shift+Z)">Redo</Button>
              <span className="wf-toolbar-sep" aria-hidden />
              <Button size="sm" icon="wand" onClick={autoLayout} title="Arrange nodes by flow depth">Auto-layout</Button>
            </>
          )}
          <Button size="sm" icon="check-circle" onClick={runValidate} title="Check the flow for structural problems">Validate</Button>
          <MenuButton actions={menuActions} label="More workflow actions" />
          {canEdit && (
            <>
              <span className="wf-toolbar-sep" aria-hidden />
              {dirty && <Button size="sm" variant="ghost" icon="x" onClick={() => setDiscardConfirm(true)} title="Throw away unsaved edits">Discard</Button>}
              {wf.status !== "approved"
                ? <Button size="sm" icon="rocket" busy={busy} onClick={() => setStatusConfirm("approved")} title="Make this flow the one live calls follow">Activate</Button>
                : <Button size="sm" icon="pause" busy={busy} onClick={() => setStatusConfirm("draft")} title="Set back to draft — live calls stop following it">Deactivate</Button>}
              <Button size="sm" variant="primary" icon="check" busy={busy} disabled={!dirty}
                title={dirty ? "Save your edits as a new version (Ctrl+S)" : "No changes to save"}
                onClick={() => void persist()}>Save version</Button>
            </>
          )}
        </div>
      </div>

      <div className="grid wf-editor-grid" style={{ gridTemplateColumns: canEdit ? "176px 1fr" : "1fr", gap: 14, alignItems: "start" }}>
        {/* Palette */}
        {canEdit && (
          <div className="card card-pad-sm col gap-4 wf-palette">
            <span className="t-label" style={{ padding: "2px 4px" }}>Node palette</span>
            {(Object.keys(nodeMeta) as NodeKind[])
              .filter((k) => k !== "start" || !startExists)
              .map((k) => (
                <button
                  key={k}
                  className="wf-palette-btn"
                  onClick={() => addNode(k)}
                  title={nodeMeta[k].desc}
                  aria-label={`Add ${nodeMeta[k].label} node`}
                >
                  <Icon name={nodeMeta[k].icon} size={13} style={{ color: nodeMeta[k].color, marginTop: 2, flex: "none" }} />
                  <span style={{ minWidth: 0 }}>
                    <span className="wf-palette-name">{nodeMeta[k].label}</span>
                    <span className="wf-palette-desc">{nodeMeta[k].desc}</span>
                  </span>
                </button>
              ))}
          </div>
        )}

        {/* Canvas */}
        <div className="wf-canvas-wrap">
          {connectFrom && (
            <span className="wf-hint">
              <Icon name="plug" size={12} />
              Click a target node to connect — Esc cancels
            </span>
          )}
          <div className="wf-canvas" ref={scrollRef} style={{ height: "clamp(460px, calc(100vh - 460px), 700px)", overflow: "auto" }} role="application" aria-label="Workflow canvas">
            {nodes.length === 0 && (
              <EmptyState
                icon="workflow"
                title="This workflow is empty"
                body={canEdit ? "Every journey begins with a Start node." : "No steps have been added yet."}
                action={canEdit ? <Button size="sm" variant="primary" icon="plus" onClick={() => addNode("start")}>Add Start node</Button> : undefined}
              />
            )}
            <div style={{ width: canvasW * zoom, height: canvasH * zoom }}>
              <div style={{ position: "relative", width: canvasW, height: canvasH, transform: `scale(${zoom})`, transformOrigin: "0 0" }}>
                <svg width={canvasW} height={canvasH} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
                  <defs>
                    <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                      <path d="M0 0 L8 4 L0 8 Z" fill="var(--axis-line)" />
                    </marker>
                    <marker id="arrow-active" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                      <path d="M0 0 L8 4 L0 8 Z" fill="var(--brand-500)" />
                    </marker>
                  </defs>
                  {edges.map((e) => {
                    const from = nodes.find((n) => n.id === e.from);
                    const to = nodes.find((n) => n.id === e.to);
                    if (!from || !to) return null;
                    const p = edgePath(from, to, Math.min(parallelIndex.get(e.id) ?? 0, 2) * 13);
                    const active = edgeIsActive(e);
                    const faded = hasSelection && !active;
                    const summary = edgeLabelSummary(e.label);
                    return (
                      <g key={e.id} style={{ opacity: faded ? 0.16 : 1, transition: "opacity 0.12s" }}>
                        <path
                          d={p.d} fill="none"
                          stroke={active ? "var(--brand-500)" : "var(--axis-line)"}
                          strokeWidth={active ? 2 : 1.6}
                          markerEnd={active ? "url(#arrow-active)" : "url(#arrow)"}
                        />
                        {/* Invisible fat stroke so the connection itself is clickable. */}
                        <path
                          d={p.d} fill="none" stroke="transparent" strokeWidth={14}
                          style={{ pointerEvents: "stroke", cursor: "pointer" }}
                          onClick={() => selectEdge(e.id)}
                        >
                          <title>{e.label ? `${e.label}` : "Connection — click to edit"}</title>
                        </path>
                        {summary && !faded && (hasSelection || showIdleLabels) && (
                          <text
                            x={p.lx} y={p.ly} textAnchor="middle"
                            style={{
                              fontSize: 10.5, fontWeight: 600,
                              fill: active ? "var(--brand-500)" : "var(--ink-3)",
                              stroke: "var(--surface-2)", strokeWidth: 3, paintOrder: "stroke",
                            }}
                          >
                            {summary}
                          </text>
                        )}
                      </g>
                    );
                  })}
                </svg>
                {nodes.map((n) => {
                  const meta = nodeMeta[n.kind];
                  const nodeIssues = issues.filter((i) => i.nodeId === n.id);
                  const worst = nodeIssues.some((i) => i.level === "error") ? "error" : nodeIssues.length ? "warning" : null;
                  const preview = nodePreview(n);
                  const mode = String((n.config ?? {}).responseMode ?? "");
                  return (
                    <button
                      key={n.id}
                      className={`wf-node${selected === n.id ? " selected" : ""}${connectFrom === n.id ? " connect-source" : ""}`}
                      style={{ left: n.x, top: n.y, width: NODE_W, height: NODE_H, touchAction: "none" }}
                      aria-pressed={selected === n.id}
                      aria-label={`${meta.label}: ${n.label}`}
                      onPointerDown={(ev) => {
                        if (!canEdit) return;
                        dragRef.current = { id: n.id, dx: ev.clientX - n.x * zoom, dy: ev.clientY - n.y * zoom, zoom, moved: false };
                        (ev.target as HTMLElement).setPointerCapture?.(ev.pointerId);
                      }}
                      onPointerMove={(ev) => {
                        const drag = dragRef.current;
                        if (!drag || drag.id !== n.id || ev.buttons === 0) return;
                        const x = Math.max(0, Math.round((ev.clientX - drag.dx) / drag.zoom));
                        const y = Math.max(0, Math.round((ev.clientY - drag.dy) / drag.zoom));
                        if (!drag.moved && Math.abs(x - n.x) + Math.abs(y - n.y) < 5) return;
                        if (!drag.moved) {
                          drag.moved = true;
                          snapshot();
                        }
                        updateNode(n.id, { x, y });
                      }}
                      onPointerUp={() => {
                        const drag = dragRef.current;
                        dragRef.current = null;
                        if (drag?.moved) return; // it was a drag, not a click
                        if (connectFrom && connectFrom !== n.id) {
                          addEdge(connectFrom, n.id);
                          setConnectFrom(null);
                          toast("Nodes connected");
                          return;
                        }
                        selectNode(n.id === selected ? null : n.id);
                      }}
                    >
                      <span className="wf-node-title">
                        <Icon name={meta.icon} size={13} style={{ color: meta.color, flex: "none" }} />
                        <span className="wf-node-label" title={n.label}>{n.label || meta.label}</span>
                        {mode === "exact" && <Icon name="lock" size={11} style={{ color: "var(--ink-3)", flex: "none" }} aria-label="Exact wording" />}
                        {mode === "llm_grounded" && <Icon name="sparkles" size={11} style={{ color: "var(--series-6)", flex: "none" }} aria-label="AI-grounded wording" />}
                        {worst && <Icon name="alert" size={12} style={{ color: worst === "error" ? "var(--viz-critical, #d64545)" : "var(--viz-warning)", flex: "none" }} />}
                      </span>
                      {preview && <span className="wf-node-sub" title={preview}>{preview}</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
          <div className="wf-zoom" role="group" aria-label="Canvas zoom">
            <button className="btn-icon" style={{ width: 26, height: 26 }} aria-label="Zoom out" title="Zoom out" onClick={() => setZoomClamped(zoom - 0.1)}>
              <Icon name="zoom-out" size={14} />
            </button>
            <span className="wf-zoom-value t-num">{Math.round(zoom * 100)}%</span>
            <button className="btn-icon" style={{ width: 26, height: 26 }} aria-label="Zoom in" title="Zoom in" onClick={() => setZoomClamped(zoom + 0.1)}>
              <Icon name="zoom-in" size={14} />
            </button>
            <button className="btn-icon" style={{ width: 26, height: 26 }} aria-label="Fit to view" title="Fit the whole flow in view" onClick={zoomToFit}>
              <Icon name="maximize" size={14} />
            </button>
          </div>
        </div>
      </div>

      {/* Inspector — a draggable floating popup, so the canvas keeps the room.
          Non-modal: the canvas stays fully interactive while it is open. */}
      {(selectedNode || selectedEdge) && createPortal(
        <div className="wf-popup" role="dialog" aria-label="Inspector"
          style={{ left: popupPosition.x, top: popupPosition.y, maxHeight: Math.max(200, window.innerHeight - popupPosition.y - 12) }}>
          <div
            className="wf-popup-head"
            title="Drag to move"
            onPointerDown={(ev) => {
              if ((ev.target as HTMLElement).closest("button")) return;
              popupDrag.current = { dx: ev.clientX - popupPosition.x, dy: ev.clientY - popupPosition.y };
              (ev.currentTarget as HTMLElement).setPointerCapture?.(ev.pointerId);
            }}
            onPointerMove={(ev) => {
              if (!popupDrag.current) return;
              setPopupPos(clampPopup(ev.clientX - popupDrag.current.dx, ev.clientY - popupDrag.current.dy));
            }}
            onPointerUp={() => { popupDrag.current = null; }}
          >
            <Icon name="grip" size={13} style={{ color: "var(--ink-3)", flex: "none" }} />
            {selectedNode ? (
              <>
                <Icon name={nodeMeta[selectedNode.kind].icon} size={14} style={{ color: nodeMeta[selectedNode.kind].color, flex: "none" }} />
                <span className="t-strong" style={{ fontSize: 13 }}>{nodeMeta[selectedNode.kind].label}</span>
                <code className="tag" title="Node id">{selectedNode.id}</code>
              </>
            ) : (
              <>
                <Icon name="plug" size={14} style={{ color: "var(--brand-500)", flex: "none" }} />
                <span className="t-strong" style={{ fontSize: 13 }}>Connection</span>
              </>
            )}
            <button className="btn-icon" style={{ marginLeft: "auto", width: 24, height: 24, flex: "none" }} aria-label="Close inspector"
              onClick={() => { setSelected(null); setSelectedEdgeId(null); }}>
              <Icon name="x" size={14} />
            </button>
          </div>
          <div className="wf-popup-body">
          {connectFrom && (
            <Callout tone="info" title="Connecting">
              Click a target node on the canvas, or{" "}
              <button style={{ textDecoration: "underline" }} onClick={() => setConnectFrom(null)}>cancel</button>.
            </Callout>
          )}

          {/* ---- Connection editor ---- */}
          {selectedEdge && !selectedNode && (() => {
            const from = nodes.find((n) => n.id === selectedEdge.from);
            const to = nodes.find((n) => n.id === selectedEdge.to);
            return (
              <>
                <div className="t-micro">
                  <button style={{ fontWeight: 650, textDecoration: "underline" }} onClick={() => selectNode(selectedEdge.from)}>{from?.label ?? selectedEdge.from}</button>
                  {" → "}
                  <button style={{ fontWeight: 650, textDecoration: "underline" }} onClick={() => selectNode(selectedEdge.to)}>{to?.label ?? selectedEdge.to}</button>
                </div>
                <label className="field">
                  <span className="field-label">Branch label (match tokens)</span>
                  <textarea className="textarea" rows={3} value={selectedEdge.label ?? ""} aria-label="Branch label"
                    disabled={!canEdit}
                    placeholder="e.g. yes,haan,ok — or true / false"
                    onChange={(ev) => {
                      setEdges((es) => es.map((x) => (x.id === selectedEdge.id ? { ...x, label: ev.target.value } : x)));
                      setDirty(true);
                    }} />
                  <span className="t-micro">
                    Intent branches match the caller's words against these tokens (separate with commas). “else” or “fallback” marks the default branch; conditions use “true” / “false”.
                  </span>
                </label>
                {canEdit && from?.kind === "condition" && (
                  <div className="row gap-6">
                    {["true", "false", "else"].map((v) => (
                      <Button key={v} size="sm" onClick={() => {
                        setEdges((es) => es.map((x) => (x.id === selectedEdge.id ? { ...x, label: v } : x)));
                        setDirty(true);
                      }}>{v}</Button>
                    ))}
                  </div>
                )}
                {canEdit && (
                  <Button size="sm" variant="danger-ghost" icon="trash" onClick={() => removeEdge(selectedEdge.id)}>
                    Delete connection
                  </Button>
                )}
              </>
            );
          })()}

          {/* ---- Node editor ---- */}
          {selectedNode && (
            <>
              <div className="wf-inspector-grid">
              <label className="field">
                <span className="field-label">Label</span>
                <input className="input" value={selectedNode.label} aria-label="Node label" disabled={!canEdit}
                  onChange={(e) => updateNode(selectedNode.id, { label: e.target.value })} />
              </label>

              {["message", "end", "handover"].includes(selectedNode.kind) && (
                <label className="field">
                  <span className="field-label">Bot says</span>
                  <textarea className="textarea" rows={2} value={configText("text")} aria-label="Bot says" disabled={!canEdit}
                    onChange={(e) => updateConfig(selectedNode.id, "text", e.target.value)} />
                </label>
              )}
              {selectedNode.kind === "ask" && (
                <>
                  <label className="field">
                    <span className="field-label">Question</span>
                    <textarea className="textarea" rows={2} value={configText("question")} aria-label="Question" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "question", e.target.value)} />
                  </label>
                  <label className="field">
                    <span className="field-label">Save answer as</span>
                    <input className="input" value={configText("variable")} aria-label="Save answer as" disabled={!canEdit}
                      placeholder="e.g. amount" onChange={(e) => updateConfig(selectedNode.id, "variable", e.target.value)} />
                  </label>
                  <label className="field">
                    <span className="field-label">Expected answer type</span>
                    <select className="select" value={configText("entityType") || "text"} aria-label="Expected answer type" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "entityType", e.target.value)}>
                      {["text", "number", "currency", "date", "time", "phone", "email"].map((t) => (
                        <option key={t} value={t}>{t}</option>
                      ))}
                    </select>
                  </label>
                </>
              )}
              {selectedNode.kind === "intent" && (
                <label className="field">
                  <span className="field-label">Prompt</span>
                  <textarea className="textarea" rows={2} value={configText("prompt")} aria-label="Prompt" disabled={!canEdit}
                    placeholder="What is this call about?" onChange={(e) => updateConfig(selectedNode.id, "prompt", e.target.value)} />
                </label>
              )}
              {selectedNode.kind === "condition" && (
                <>
                  <label className="field">
                    <span className="field-label">Variable</span>
                    <input className="input" value={configText("variable")} aria-label="Condition variable" disabled={!canEdit}
                      placeholder="e.g. amount" onChange={(e) => updateConfig(selectedNode.id, "variable", e.target.value)} />
                  </label>
                  <label className="field">
                    <span className="field-label">Operator</span>
                    <select className="select" value={configText("operator") || "exists"} aria-label="Condition operator" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "operator", e.target.value)}>
                      {["exists", "equals", "not_equals", "contains", "gte", "lte", "gt", "lt"].map((op) => (
                        <option key={op} value={op}>{op}</option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span className="field-label">Value</span>
                    <input className="input" value={configText("value")} aria-label="Condition value" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "value", e.target.value)} />
                  </label>
                  <p className="t-micro">
                    Label the outgoing connections “true” and “false” to branch.
                  </p>
                </>
              )}
              {selectedNode.kind === "knowledge" && (
                <>
                  <label className="field">
                    <span className="field-label">Query template (optional)</span>
                    <input className="input" value={configText("query")} aria-label="Query template" disabled={!canEdit}
                      placeholder="Defaults to the caller's words" onChange={(e) => updateConfig(selectedNode.id, "query", e.target.value)} />
                  </label>
                  <label className="field">
                    <span className="field-label">If no answer found</span>
                    <input className="input" value={configText("fallbackText")} aria-label="If no answer found" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "fallbackText", e.target.value)} />
                  </label>
                </>
              )}
              {selectedNode.kind === "api" && (
                <>
                  <label className="field">
                    <span className="field-label">Action name</span>
                    <input className="input" value={configText("name")} aria-label="Action name" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "name", e.target.value)} />
                  </label>
                  <label className="field">
                    <span className="field-label">On failure</span>
                    <select className="select" value={configText("onFailure") || "handover"} aria-label="On failure" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "onFailure", e.target.value)}>
                      <option value="handover">Route to handover</option>
                      <option value="retry">Retry once, then apologise</option>
                      <option value="skip">Skip and continue</option>
                    </select>
                  </label>
                </>
              )}

              {/* Delivery mode — how the authored text is spoken at runtime. */}
              {SPEAKING_KINDS.includes(selectedNode.kind) && (
                <>
                  <label className="field">
                    <span className="field-label">Delivery</span>
                    <select className="select" value={configText("responseMode") || "fixed"} aria-label="Delivery mode" disabled={!canEdit}
                      onChange={(e) => updateConfig(selectedNode.id, "responseMode", e.target.value)}>
                      <option value="fixed">Fixed — authored text, language-adapted</option>
                      <option value="exact">Exact — spoken verbatim, never adapted</option>
                      <option value="llm_grounded">AI grounded — LLM words it naturally</option>
                    </select>
                    <span className="t-micro">
                      {configText("responseMode") === "exact"
                        ? "For legal / compliance wording. The text is never paraphrased or translated."
                        : configText("responseMode") === "llm_grounded"
                          ? "The LLM rewords this step from the directive below; the authored text stays the fallback."
                          : "Default. The authored text is spoken, adapted to the caller's language."}
                    </span>
                  </label>
                  {configText("responseMode") === "llm_grounded" && (
                    <>
                      <label className="field">
                        <span className="field-label">Response directive</span>
                        <textarea className="textarea" rows={2} value={configText("responseDirective")} aria-label="Response directive" disabled={!canEdit}
                          placeholder="What the reply must accomplish, e.g. state the amount and ask for payment now"
                          onChange={(e) => updateConfig(selectedNode.id, "responseDirective", e.target.value)} />
                      </label>
                      <label className="field">
                        <span className="field-label">Must include (comma-separated)</span>
                        <input className="input" value={mustIncludeValue} aria-label="Must include" disabled={!canEdit}
                          placeholder="e.g. ₹2000, UPI"
                          onChange={(e) => {
                            setMustIncludeDraft(e.target.value);
                            updateConfig(selectedNode.id, "responseMustInclude",
                              e.target.value.split(",").map((s) => s.trim()).filter(Boolean));
                          }} />
                        <span className="t-micro">Literals the generated reply must contain (IDs, amounts, names) — keep them language-neutral.</span>
                      </label>
                    </>
                  )}
                </>
              )}

              {/* Connections */}
              <div className="col gap-6" style={{ gridColumn: "1 / -1" }}>
                <span className="field-label">Branches from this node</span>
                {outgoing.length === 0 && <span className="t-micro">None yet.</span>}
                <div className="wf-branch-grid">
                {outgoing.map((e, idx) => {
                  const target = nodes.find((n) => n.id === e.to);
                  return (
                    <div key={e.id} className="row gap-4">
                      {canEdit && (
                        <span className="col" style={{ gap: 0 }}>
                          <button className="btn-icon" style={{ width: 18, height: 13 }} disabled={idx === 0}
                            aria-label={`Move branch to ${target?.label ?? e.to} up`} title="Try this branch earlier"
                            onClick={() => moveEdge(e.id, -1)}>
                            <Icon name="chevron-up" size={11} />
                          </button>
                          <button className="btn-icon" style={{ width: 18, height: 13 }} disabled={idx === outgoing.length - 1}
                            aria-label={`Move branch to ${target?.label ?? e.to} down`} title="Try this branch later"
                            onClick={() => moveEdge(e.id, 1)}>
                            <Icon name="chevron-down" size={11} />
                          </button>
                        </span>
                      )}
                      <button className="t-micro" style={{ minWidth: 56, maxWidth: 88, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", textAlign: "left" }}
                        title={`Select ${target?.label ?? e.to}`} onClick={() => selectNode(e.to)}>
                        → {target?.label ?? e.to}
                      </button>
                      <input className="input" style={{ flex: 1, minWidth: 0 }} value={e.label ?? ""} placeholder="branch label" disabled={!canEdit}
                        aria-label={`Label for connection to ${target?.label ?? e.to}`}
                        onChange={(ev) => {
                          setEdges((es) => es.map((x) => (x.id === e.id ? { ...x, label: ev.target.value } : x)));
                          setDirty(true);
                        }} />
                      {canEdit && (
                        <Button size="sm" variant="danger-ghost" icon="trash" title="Remove connection"
                          onClick={() => removeEdge(e.id)} aria-label={`Remove connection to ${target?.label ?? e.to}`} />
                      )}
                    </div>
                  );
                })}
                </div>
                <div className="row gap-10 wrap">
                  {canEdit && (
                    <Button size="sm" icon="plug" onClick={() => setConnectFrom(selectedNode.id)}>
                      Connect to…
                    </Button>
                  )}
                  {outgoing.length > 1 && (
                    <span className="t-micro">Branches are tried top to bottom — put the success branch first.</span>
                  )}
                </div>
              </div>

              {incoming.length > 0 && (
                <div className="col gap-4">
                  <span className="field-label">Arrives from</span>
                  <div className="row gap-6 wrap">
                    {incoming.map((e) => {
                      const src = nodes.find((n) => n.id === e.from);
                      return (
                        <button key={e.id} className="tag" style={{ cursor: "pointer" }}
                          title={e.label ? `via: ${e.label}` : "Select source node"} onClick={() => selectNode(e.from)}>
                          ← {src?.label ?? e.from}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {selectedIssues.map((i, ix) => (
                <div key={ix} className="callout callout-warning" style={{ gridColumn: "1 / -1", padding: "9px 11px", fontSize: 12 }}>
                  <Icon name="alert" size={13} />
                  <div className="callout-body">{i.message}</div>
                </div>
              ))}
              </div>
              {canEdit && (
                <div className="row gap-6">
                  {selectedNode.kind !== "start" && (
                    <Button size="sm" icon="copy" title="Duplicate this node (without connections)"
                      onClick={() => duplicateNode(selectedNode.id)}>
                      Duplicate
                    </Button>
                  )}
                  <Button size="sm" variant="danger-ghost" icon="trash" disabled={selectedNode.kind === "start"}
                    title={selectedNode.kind === "start" ? "The start node cannot be removed" : "Delete node (Del)"}
                    onClick={() => deleteNode(selectedNode.id)}>
                    Delete node
                  </Button>
                </div>
              )}
            </>
          )}
          </div>
        </div>,
        document.body,
      )}

      {issues.length > 0 && (
        <Callout tone={errorCount ? "critical" : "warning"}
          title={`${issues.length} validation ${issues.length === 1 ? "issue" : "issues"}${errorCount ? ` (${errorCount} blocking)` : ""}${warningCount && errorCount ? ` · ${warningCount} warnings` : ""}`}>
          {issues.map((i, ix) => {
            const node = nodes.find((n) => n.id === i.nodeId);
            return (
              <div key={ix} className="row gap-6" style={{ marginTop: ix ? 4 : 0 }}>
                <button style={{ fontWeight: 650, textDecoration: "underline" }} onClick={() => selectNode(i.nodeId)}>
                  {node?.label ?? i.nodeId}
                </button>
                — {i.message}
              </div>
            );
          })}
        </Callout>
      )}

      {/* Rename */}
      <Modal open={renameOpen} onClose={() => setRenameOpen(false)} title="Rename workflow"
        sub="The name identifies this journey in routing and exports"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenameOpen(false)}>Cancel</Button>
            <Button variant="primary" busy={busy} disabled={!renameValue.trim() || renameValue.trim() === wf.name}
              onClick={() => { setRenameOpen(false); void persist({ name: renameValue.trim() }); }}>
              Rename & save
            </Button>
          </>
        }>
        <div className="col gap-10">
          <label className="field">
            <span className="field-label">Workflow name</span>
            <input className="input" value={renameValue} maxLength={200} aria-label="Workflow name" autoFocus
              onChange={(e) => setRenameValue(e.target.value)} />
          </label>
          <span className="t-micro">
            Route string: <code className="tag">workflow:{slugifyWorkflowName(renameValue.trim() || wf.name) || "…"}</code>
          </span>
          <Callout tone="warning" title="Renaming can break intent routes">
            Intent routes that reference this workflow by name (<code>workflow:{slugifyWorkflowName(wf.name)}</code>) will stop
            matching. Routes that use the workflow id (<code>workflow:{wf.id}</code>) keep working — prefer those.
          </Callout>
        </div>
      </Modal>

      {/* Activate / deactivate confirmation */}
      <ConfirmModal
        open={statusConfirm !== null}
        onClose={() => setStatusConfirm(null)}
        busy={busy}
        title={statusConfirm === "approved" ? "Activate this workflow?" : "Deactivate this workflow?"}
        body={statusConfirm === "approved"
          ? <>Your current canvas {dirty ? "(including unsaved edits) " : ""}is saved as a new version and marked <b>Active</b> — live calls and the Testing tab follow it within about 30 seconds.</>
          : <>The workflow is saved as a new <b>Draft</b> version{dirty ? " (including unsaved edits)" : ""}. Live calls stop following this journey until it is activated again.</>}
        confirmLabel={statusConfirm === "approved" ? "Activate" : "Deactivate"}
        onConfirm={() => {
          const status = statusConfirm!;
          setStatusConfirm(null);
          void persist({ status });
        }}
      />

      {/* Discard unsaved edits */}
      <ConfirmModal
        open={discardConfirm}
        onClose={() => setDiscardConfirm(false)}
        danger
        title="Discard unsaved changes?"
        body={<>Your edits since the last save are thrown away and the canvas reloads workflow v{wf.version} from the server.</>}
        confirmLabel="Discard changes"
        onConfirm={() => { setDiscardConfirm(false); q.reload(); }}
      />

      {/* Import replaces the current canvas */}
      <ConfirmModal
        open={pendingImport !== null}
        onClose={() => setPendingImport(null)}
        danger
        title="Replace the canvas with the imported flow?"
        body={<>The imported document has <b>{pendingImport?.nodes.length ?? 0} nodes</b> and <b>{pendingImport?.edges.length ?? 0} connections</b>. It replaces everything on the canvas (undo is available) and nothing is saved until you Save version.</>}
        confirmLabel="Replace canvas"
        onConfirm={() => pendingImport && applyImport(pendingImport)}
      />
    </div>
  );
}
