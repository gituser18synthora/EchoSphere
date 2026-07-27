/* Workflow builder — a real editor over the bot's saved journey.

   Nodes and edges live in local editable state (loaded from the server
   document); the palette adds nodes, nodes drag to move, the inspector edits
   each node's label + runtime configuration and manages its connections.
   Save PUTs the document; the backend validates it structurally, stores
   server-computed issues, and the SAME saved graph is what the runtime
   workflow engine executes on live calls and in the Testing tab. */

import { useEffect, useMemo, useRef, useState } from "react";
import type { NodeKind, VoiceBot, WorkflowEdge, WorkflowNode } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { getWorkflow, saveWorkflow } from "@/services/api";
import { Button, Callout, CardSkeleton, ErrorState, StatusChip } from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const nodeMeta: Record<NodeKind, { icon: IconName; color: string; label: string }> = {
  start: { icon: "play", color: "var(--series-2)", label: "Start" },
  message: { icon: "message", color: "var(--series-1)", label: "Message" },
  ask: { icon: "help", color: "var(--series-6)", label: "Ask (collect)" },
  intent: { icon: "target", color: "var(--series-4)", label: "Intent router" },
  condition: { icon: "workflow", color: "var(--series-3)", label: "Condition" },
  api: { icon: "zap", color: "var(--series-8)", label: "API call" },
  knowledge: { icon: "book", color: "var(--series-5)", label: "Knowledge answer" },
  handover: { icon: "headphones", color: "var(--series-7)", label: "Human handover" },
  end: { icon: "check-circle", color: "var(--ink-3)", label: "End" },
};

const NODE_W = 178;
const NODE_H = 54;

type Issue = { nodeId: string; level: "warning" | "error"; message: string };

const newId = (prefix: string) => `${prefix}_${Math.random().toString(36).slice(2, 8)}`;

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

export default function WorkflowsTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => getWorkflow(bot.id), [bot.id]);
  const { toast } = useApp();

  const [nodes, setNodes] = useState<WorkflowNode[]>([]);
  const [edges, setEdges] = useState<WorkflowEdge[]>([]);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [connectFrom, setConnectFrom] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [past, setPast] = useState<{ nodes: WorkflowNode[]; edges: WorkflowEdge[] }[]>([]);
  const [future, setFuture] = useState<{ nodes: WorkflowNode[]; edges: WorkflowEdge[] }[]>([]);

  /* Load the server document into the editable state. */
  useEffect(() => {
    if (q.data) {
      setNodes(q.data.nodes);
      setEdges(q.data.edges);
      setIssues(q.data.issues as Issue[]);
      setPast([]);
      setFuture([]);
      setDirty(false);
      setSelected(null);
      setConnectFrom(null);
    }
  }, [q.data]);

  /* Drag-to-move: pointer capture on the node, position updates on move. */
  const dragRef = useRef<{ id: string; dx: number; dy: number; moved: boolean } | null>(null);

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
  const selectedIssues = issues.filter((i) => i.nodeId === selected);
  const outgoing = useMemo(() => edges.filter((e) => e.from === selected), [edges, selected]);

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading || !wf) return <CardSkeleton rows={10} />;

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
    setSelected(id);
    toast(`${nodeMeta[kind].label} node added — connect it from another node`);
  };

  const deleteNode = (id: string) => {
    snapshot();
    setNodes((ns) => ns.filter((n) => n.id !== id));
    setEdges((es) => es.filter((e) => e.from !== id && e.to !== id));
    setSelected(null);
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

  const persist = async (status?: "draft" | "approved") => {
    if (busy) return;
    setBusy(true);
    try {
      const saved = await saveWorkflow(bot.id, {
        nodes, edges, ...(status ? { status } : {}),
      });
      setIssues(saved.issues as Issue[]);
      setDirty(false);
      toast(status === "approved"
        ? `Workflow v${saved.version} activated`
        : status === "draft"
          ? `Workflow v${saved.version} deactivated (draft)`
          : `Workflow saved as v${saved.version} ${saved.status}`);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not save workflow", "error");
    } finally {
      setBusy(false);
    }
  };

  const edgePath = (from: WorkflowNode, to: WorkflowNode) => {
    const x1 = from.x + NODE_W;
    const y1 = from.y + NODE_H / 2;
    const x2 = to.x;
    const y2 = to.y + NODE_H / 2;
    if (to.x < from.x + NODE_W && Math.abs(to.y - from.y) > NODE_H) {
      const vx1 = from.x + NODE_W / 2;
      const vy1 = from.y + NODE_H;
      const vx2 = to.x + NODE_W / 2;
      const vy2 = to.y;
      return { d: `M${vx1} ${vy1} C ${vx1} ${vy1 + 34}, ${vx2} ${vy2 - 34}, ${vx2} ${vy2}`, lx: (vx1 + vx2) / 2, ly: (vy1 + vy2) / 2 };
    }
    const mx = (x1 + x2) / 2;
    return { d: `M${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`, lx: mx, ly: (y1 + y2) / 2 - 6 };
  };

  const canvasW = Math.max(660, ...nodes.map((n) => n.x + NODE_W + 60));
  const canvasH = Math.max(570, ...nodes.map((n) => n.y + NODE_H + 60));
  const configText = (key: string): string => String((selectedNode?.config ?? {})[key] ?? "");

  return (
    <div className="col gap-16">
      <div className="row-between wrap">
        <div className="row gap-8">
          <span className="t-strong" style={{ fontSize: 14 }}>{wf.name}</span>
          <code className="tag">v{wf.version}</code>
          <StatusChip status={wf.status} />
          {dirty && <span className="tag" title="You have unsaved edits">Unsaved changes</span>}
          <span className="t-micro">edited by {wf.updatedBy} · {new Date(wf.updatedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
        </div>
        <div className="row gap-6">
          <Button size="sm" icon="undo" disabled={past.length === 0} onClick={undo}>Undo</Button>
          <Button size="sm" icon="redo" disabled={future.length === 0} onClick={redo}>Redo</Button>
          <Button size="sm" icon="wand" onClick={autoLayout}>Auto-layout</Button>
          <Button size="sm" icon="check-circle" onClick={runValidate}>Validate</Button>
          {wf.status !== "approved"
            ? <Button size="sm" icon="rocket" onClick={() => void persist("approved")}>Activate</Button>
            : <Button size="sm" icon="pause" onClick={() => void persist("draft")}>Deactivate</Button>}
          <Button size="sm" variant="primary" icon="check" busy={busy} onClick={() => void persist()}>Save version</Button>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "170px 1fr 300px", gap: 14, alignItems: "start" }}>
        {/* Palette */}
        <div className="card card-pad-sm col gap-4">
          <span className="t-label" style={{ padding: "2px 4px" }}>Node palette</span>
          {(Object.keys(nodeMeta) as NodeKind[])
            .filter((k) => k !== "start" || !nodes.some((n) => n.kind === "start"))
            .map((k) => (
              <button
                key={k}
                className="row gap-8"
                style={{ padding: "7px 8px", borderRadius: 8, fontSize: 12.5, fontWeight: 550, border: "1px dashed var(--border)", cursor: "pointer" }}
                onClick={() => addNode(k)}
                title={`Add ${nodeMeta[k].label} node`}
                aria-label={`Add ${nodeMeta[k].label} node`}
              >
                <Icon name={nodeMeta[k].icon} size={13} style={{ color: nodeMeta[k].color }} />
                {nodeMeta[k].label}
              </button>
            ))}
        </div>

        {/* Canvas */}
        <div className="wf-canvas" style={{ height: 570, overflow: "auto" }} role="application" aria-label="Workflow canvas">
          {nodes.length === 0 && (
            <p className="t-sub" style={{ padding: 24 }}>
              This workflow is empty — add a Start node from the palette to begin.
            </p>
          )}
          <div style={{ position: "relative", width: canvasW, height: canvasH }}>
            <svg width={canvasW} height={canvasH} style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
              <defs>
                <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M0 0 L8 4 L0 8 Z" fill="var(--axis-line)" />
                </marker>
              </defs>
              {edges.map((e) => {
                const from = nodes.find((n) => n.id === e.from);
                const to = nodes.find((n) => n.id === e.to);
                if (!from || !to) return null;
                const p = edgePath(from, to);
                return (
                  <g key={e.id}>
                    <path d={p.d} fill="none" stroke="var(--axis-line)" strokeWidth={1.6} markerEnd="url(#arrow)" />
                    {e.label && (
                      <text x={p.lx} y={p.ly} textAnchor="middle" style={{ fontSize: 10.5, fill: "var(--ink-3)", fontWeight: 600 }}>
                        {e.label}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
            {nodes.map((n) => {
              const meta = nodeMeta[n.kind];
              const hasIssue = issues.some((i) => i.nodeId === n.id);
              return (
                <button
                  key={n.id}
                  className={`wf-node${selected === n.id ? " selected" : ""}${connectFrom === n.id ? " selected" : ""}`}
                  style={{ left: n.x, top: n.y, width: NODE_W, minHeight: NODE_H, touchAction: "none" }}
                  aria-pressed={selected === n.id}
                  onPointerDown={(ev) => {
                    dragRef.current = { id: n.id, dx: ev.clientX - n.x, dy: ev.clientY - n.y, moved: false };
                    (ev.target as HTMLElement).setPointerCapture?.(ev.pointerId);
                  }}
                  onPointerMove={(ev) => {
                    const drag = dragRef.current;
                    if (!drag || drag.id !== n.id || ev.buttons === 0) return;
                    const x = Math.max(0, Math.round(ev.clientX - drag.dx));
                    const y = Math.max(0, Math.round(ev.clientY - drag.dy));
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
                    setSelected(n.id === selected ? null : n.id);
                  }}
                >
                  <span className="wf-node-title">
                    <Icon name={meta.icon} size={13} style={{ color: meta.color }} />
                    {n.label}
                    {hasIssue && <Icon name="alert" size={12} style={{ color: "var(--viz-warning)", marginLeft: "auto" }} />}
                  </span>
                  {n.sub && <span className="wf-node-sub">{n.sub}</span>}
                </button>
              );
            })}
          </div>
        </div>

        {/* Inspector */}
        <div className="card card-pad-sm col gap-10" style={{ minHeight: 300 }}>
          <span className="t-label" style={{ padding: "2px 4px" }}>Inspector</span>
          {connectFrom && (
            <Callout tone="info" title="Connecting">
              Click a target node on the canvas, or{" "}
              <button style={{ textDecoration: "underline" }} onClick={() => setConnectFrom(null)}>cancel</button>.
            </Callout>
          )}
          {!selectedNode && !connectFrom && (
            <p className="t-sub" style={{ padding: 4, fontSize: 12.5 }}>
              Select a node to edit its configuration, or add one from the palette.
            </p>
          )}
          {selectedNode && (
            <>
              <div className="row gap-8" style={{ padding: "0 4px" }}>
                <Icon name={nodeMeta[selectedNode.kind].icon} size={15} style={{ color: nodeMeta[selectedNode.kind].color }} />
                <span className="t-strong" style={{ fontSize: 13.5 }}>{nodeMeta[selectedNode.kind].label}</span>
              </div>
              <label className="field" style={{ padding: "0 4px" }}>
                <span className="field-label">Label</span>
                <input className="input" value={selectedNode.label} aria-label="Node label"
                  onChange={(e) => updateNode(selectedNode.id, { label: e.target.value })} />
              </label>

              {["message", "end", "handover"].includes(selectedNode.kind) && (
                <label className="field" style={{ padding: "0 4px" }}>
                  <span className="field-label">Bot says</span>
                  <textarea className="textarea" rows={2} value={configText("text")} aria-label="Bot says"
                    onChange={(e) => updateConfig(selectedNode.id, "text", e.target.value)} />
                </label>
              )}
              {selectedNode.kind === "ask" && (
                <>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Question</span>
                    <textarea className="textarea" rows={2} value={configText("question")} aria-label="Question"
                      onChange={(e) => updateConfig(selectedNode.id, "question", e.target.value)} />
                  </label>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Save answer as</span>
                    <input className="input" value={configText("variable")} aria-label="Save answer as"
                      placeholder="e.g. amount" onChange={(e) => updateConfig(selectedNode.id, "variable", e.target.value)} />
                  </label>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Expected answer type</span>
                    <select className="select" value={configText("entityType") || "text"} aria-label="Expected answer type"
                      onChange={(e) => updateConfig(selectedNode.id, "entityType", e.target.value)}>
                      {["text", "number", "currency", "date", "time", "phone", "email"].map((t) => (
                        <option key={t} value={t}>{t}</option>
                      ))}
                    </select>
                  </label>
                </>
              )}
              {selectedNode.kind === "intent" && (
                <label className="field" style={{ padding: "0 4px" }}>
                  <span className="field-label">Prompt</span>
                  <textarea className="textarea" rows={2} value={configText("prompt")} aria-label="Prompt"
                    placeholder="What is this call about?" onChange={(e) => updateConfig(selectedNode.id, "prompt", e.target.value)} />
                </label>
              )}
              {selectedNode.kind === "condition" && (
                <>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Variable</span>
                    <input className="input" value={configText("variable")} aria-label="Condition variable"
                      placeholder="e.g. amount" onChange={(e) => updateConfig(selectedNode.id, "variable", e.target.value)} />
                  </label>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Operator</span>
                    <select className="select" value={configText("operator") || "exists"} aria-label="Condition operator"
                      onChange={(e) => updateConfig(selectedNode.id, "operator", e.target.value)}>
                      {["exists", "equals", "not_equals", "contains", "gte", "lte", "gt", "lt"].map((op) => (
                        <option key={op} value={op}>{op}</option>
                      ))}
                    </select>
                  </label>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Value</span>
                    <input className="input" value={configText("value")} aria-label="Condition value"
                      onChange={(e) => updateConfig(selectedNode.id, "value", e.target.value)} />
                  </label>
                  <p className="t-micro" style={{ padding: "0 4px" }}>
                    Label the outgoing connections “true” and “false” to branch.
                  </p>
                </>
              )}
              {selectedNode.kind === "knowledge" && (
                <>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Query template (optional)</span>
                    <input className="input" value={configText("query")} aria-label="Query template"
                      placeholder="Defaults to the caller's words" onChange={(e) => updateConfig(selectedNode.id, "query", e.target.value)} />
                  </label>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">If no answer found</span>
                    <input className="input" value={configText("fallbackText")} aria-label="If no answer found"
                      onChange={(e) => updateConfig(selectedNode.id, "fallbackText", e.target.value)} />
                  </label>
                </>
              )}
              {selectedNode.kind === "api" && (
                <>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">Action name</span>
                    <input className="input" value={configText("name")} aria-label="Action name"
                      onChange={(e) => updateConfig(selectedNode.id, "name", e.target.value)} />
                  </label>
                  <label className="field" style={{ padding: "0 4px" }}>
                    <span className="field-label">On failure</span>
                    <select className="select" value={configText("onFailure") || "handover"} aria-label="On failure"
                      onChange={(e) => updateConfig(selectedNode.id, "onFailure", e.target.value)}>
                      <option value="handover">Route to handover</option>
                      <option value="retry">Retry once, then apologise</option>
                      <option value="skip">Skip and continue</option>
                    </select>
                  </label>
                </>
              )}
              {selectedNode.kind === "handover" && (
                <label className="field" style={{ padding: "0 4px" }}>
                  <span className="field-label">Agent queue</span>
                  <input className="input" value={configText("queue")} aria-label="Agent queue"
                    placeholder="e.g. billing" onChange={(e) => updateConfig(selectedNode.id, "queue", e.target.value)} />
                </label>
              )}

              {/* Connections */}
              <div className="col gap-6" style={{ padding: "0 4px" }}>
                <span className="field-label">Connections from this node</span>
                {outgoing.length === 0 && <span className="t-micro">None yet.</span>}
                {outgoing.map((e) => {
                  const target = nodes.find((n) => n.id === e.to);
                  return (
                    <div key={e.id} className="row gap-6">
                      <span className="t-micro" style={{ minWidth: 60 }}>→ {target?.label ?? e.to}</span>
                      <input className="input" style={{ flex: 1 }} value={e.label ?? ""} placeholder="branch label"
                        aria-label={`Label for connection to ${target?.label ?? e.to}`}
                        onChange={(ev) => {
                          setEdges((es) => es.map((x) => (x.id === e.id ? { ...x, label: ev.target.value } : x)));
                          setDirty(true);
                        }} />
                      <Button size="sm" variant="danger-ghost" icon="trash" title="Remove connection"
                        onClick={() => removeEdge(e.id)} aria-label={`Remove connection to ${target?.label ?? e.to}`} />
                    </div>
                  );
                })}
                <Button size="sm" icon="plug" onClick={() => setConnectFrom(selectedNode.id)}>
                  Connect to…
                </Button>
              </div>

              {selectedIssues.map((i, ix) => (
                <div key={ix} className="callout callout-warning" style={{ padding: "9px 11px", fontSize: 12 }}>
                  <Icon name="alert" size={13} />
                  <div className="callout-body">{i.message}</div>
                </div>
              ))}
              <Button size="sm" variant="danger-ghost" icon="trash" disabled={selectedNode.kind === "start"}
                title={selectedNode.kind === "start" ? "The start node cannot be removed" : undefined}
                onClick={() => deleteNode(selectedNode.id)}>
                Delete node
              </Button>
            </>
          )}
        </div>
      </div>

      {issues.length > 0 && (
        <Callout tone="warning" title={`${issues.length} validation ${issues.length === 1 ? "issue" : "issues"}`}>
          {issues.map((i, ix) => {
            const node = nodes.find((n) => n.id === i.nodeId);
            return (
              <div key={ix} className="row gap-6" style={{ marginTop: ix ? 4 : 0 }}>
                <button style={{ fontWeight: 650, textDecoration: "underline" }} onClick={() => setSelected(i.nodeId)}>
                  {node?.label ?? i.nodeId}
                </button>
                — {i.message}
              </div>
            );
          })}
        </Callout>
      )}
    </div>
  );
}
