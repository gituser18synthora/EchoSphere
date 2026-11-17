import { describe, expect, it } from "vitest";
import type { WorkflowEdge, WorkflowNode } from "@/types/domain";
import {
  buildWorkflowCanvas, createWorkflowEdgePath, WORKFLOW_NODE_HEIGHT, WORKFLOW_NODE_WIDTH,
} from "./workflowCanvas";

const node = (id: string, x: number, y: number): WorkflowNode => ({
  id, kind: "message", label: id, x, y, config: { text: `Say ${id}`, custom: { retained: true } },
});
const missing = (id: string): WorkflowNode => ({ id, kind: "intent", label: id } as WorkflowNode);

describe("workflow canvas presentation", () => {
  it("keeps saved positions, advanced data and edge precedence unchanged", () => {
    const nodes = [node("a", 40, 40), node("b", 300, 150)];
    const edges = [
      { id: "first", from: "a", to: "b", label: "yes" },
      { id: "fallback", from: "a", to: "b", label: "else" },
    ];
    const before = structuredClone({ nodes, edges });
    const canvas = buildWorkflowCanvas(nodes, edges);
    expect(canvas.offsetX).toBe(0);
    expect(canvas.offsetY).toBe(0);
    expect(canvas.nodes).toEqual(nodes);
    expect(canvas.nodes[0]).not.toBe(nodes[0]);
    expect(canvas.nodes[0].config).toBe(nodes[0].config);
    expect([...canvas.edgePaths.keys()]).toEqual(["first", "fallback"]);
    expect({ nodes, edges }).toEqual(before);
  });

  it("gives missing/non-finite coordinates deterministic, non-overlapping display positions", () => {
    const nodes = [node("saved", 40, 40), missing("missing-one"), missing("missing-two"),
      node("bad", Number.NaN, Number.POSITIVE_INFINITY), node("partial-x", 40, Number.NaN),
      node("partial-y", Number.NaN, 40)];
    const before = structuredClone(nodes);
    const first = buildWorkflowCanvas(nodes, []);
    expect(buildWorkflowCanvas(nodes, []).nodes).toEqual(first.nodes);
    expect(first.nodes[0]).toEqual(nodes[0]);
    expect(first.nodeById.get("partial-x")!.x).toBe(40);
    expect(first.nodeById.get("partial-y")!.y).toBe(40);
    for (const n of first.nodes) {
      expect(Number.isFinite(n.x) && Number.isFinite(n.y)).toBe(true);
      for (const other of first.nodes) {
        if (n.id === other.id) continue;
        const overlaps = n.x < other.x + WORKFLOW_NODE_WIDTH && n.x + WORKFLOW_NODE_WIDTH > other.x
          && n.y < other.y + WORKFLOW_NODE_HEIGHT && n.y + WORKFLOW_NODE_HEIGHT > other.y;
        expect(overlaps).toBe(false);
      }
    }
    expect(nodes).toEqual(before);
    expect(nodes[1]).not.toHaveProperty("x");
    expect(Number.isNaN(nodes[3].x)).toBe(true);
    expect(Number.isFinite(first.width) && Number.isFinite(first.height)).toBe(true);
  });

  it("includes negative authored positions and complete loop curves in its bounds", () => {
    const nodes = [node("negative", -170, -200), node("overlap", -220, -200)];
    const edges = [
      { id: "loop", from: "negative", to: "negative" },
      { id: "overlap", from: "negative", to: "overlap" },
    ];
    const canvas = buildWorkflowCanvas(nodes, edges);
    for (const n of canvas.nodes) {
      expect(n.x).toBeGreaterThanOrEqual(0);
      expect(n.y).toBeGreaterThanOrEqual(0);
      expect(n.x - canvas.offsetX).toBe(nodes.find((original) => original.id === n.id)!.x);
      expect(n.y - canvas.offsetY).toBe(nodes.find((original) => original.id === n.id)!.y);
    }
    for (const p of canvas.edgePaths.values()) {
      expect(p.bounds.minX).toBeGreaterThanOrEqual(0);
      expect(p.bounds.minY).toBeGreaterThanOrEqual(0);
      expect(p.bounds.maxX).toBeLessThan(canvas.width);
      expect(p.bounds.maxY).toBeLessThan(canvas.height);
    }
  });

  it("can retain a fixed display origin during dragging", () => {
    const before = buildWorkflowCanvas([node("a", -170, -200)], []);
    const during = buildWorkflowCanvas([node("a", -160, -190)], [], before);
    expect(during.offsetX).toBe(before.offsetX);
    expect(during.offsetY).toBe(before.offsetY);
    expect(during.nodes[0].x - before.nodes[0].x).toBe(10);
    expect(during.nodes[0].y - before.nodes[0].y).toBe(10);
  });

  it("does not shift untouched fallback nodes after another is explicitly positioned", () => {
    const nodes = [node("saved", 40, 40), missing("a"), missing("b")];
    const before = buildWorkflowCanvas(nodes, []);
    const anchors = new Map(before.nodes.slice(1).map((n) => [n.id, {
      x: n.x - before.offsetX, y: n.y - before.offsetY,
    }]));
    const a = anchors.get("a")!;
    const moved = nodes.map((n) => n.id === "a" ? { ...n, x: a.x + 10, y: a.y } : n);
    const after = buildWorkflowCanvas(moved, [], before, anchors);
    expect(after.nodeById.get("a")!.x).toBe(before.nodeById.get("a")!.x + 10);
    expect(after.nodeById.get("b")).toEqual(before.nodeById.get("b"));
    expect(moved[2]).not.toHaveProperty("x");
  });

  it("reserves retained fallback anchors before positioning newly missing nodes", () => {
    const nodes = [missing("new"), missing("retained")];
    const anchors = new Map([["retained", { x: 40, y: 40 }]]);
    const canvas = buildWorkflowCanvas(nodes, [], undefined, anchors);
    expect(canvas.nodeById.get("retained")!.x).toBe(40);
    expect(canvas.nodeById.get("retained")!.y).toBe(40);
    expect(canvas.nodeById.get("new")!.y).toBeGreaterThanOrEqual(40 + WORKFLOW_NODE_HEIGHT);
    expect(anchors.get("retained")).toEqual({ x: 40, y: 40 });
  });

  it("honors authored axes over retained fallbacks without moving existing anchors", () => {
    const nodes = [node("saved", 40, 40), node("x", 60, Number.NaN),
      node("y", Number.NaN, 70), missing("overlapping")];
    const anchors = new Map([
      ["saved", { x: 900, y: 900 }], ["x", { x: 500, y: 200 }],
      ["y", { x: 700, y: 300 }], ["overlapping", { x: 40, y: 40 }],
    ]);
    const canvas = buildWorkflowCanvas(nodes, [], undefined, anchors);
    expect(canvas.nodeById.get("saved")).toEqual(nodes[0]);
    expect(canvas.nodeById.get("x")!.x).toBe(60);
    expect(canvas.nodeById.get("x")!.y).toBe(200);
    expect(canvas.nodeById.get("y")!.x).toBe(700);
    expect(canvas.nodeById.get("y")!.y).toBe(70);
    // Existing anchors may overlap after an explicit drag; keeping them in
    // place prevents a view-only projection from rearranging other nodes.
    expect(canvas.nodeById.get("overlapping")!.x).toBe(40);
    expect(canvas.nodeById.get("overlapping")!.y).toBe(40);
  });

  it("keeps every connector renderable in a dense legacy graph with missing positions", () => {
    const nodes = Array.from({ length: 141 }, (_, index) => index < 133
      ? node(`n${index}`, -170 + (index % 9) * 260, -200 + Math.floor(index / 9) * 120)
      : missing(`n${index}`));
    const edges: WorkflowEdge[] = Array.from({ length: 1152 }, (_, index) => ({
      id: `e${index}`, from: `n${index % nodes.length}`, to: `n${(index * 7 + 3) % nodes.length}`,
      label: index % 2 ? "else" : "yes",
    }));
    const original = structuredClone({ nodes, edges });
    const canvas = buildWorkflowCanvas(nodes, edges);
    expect(canvas.nodes).toHaveLength(141);
    expect(canvas.edgePaths.size).toBe(1152);
    expect([...canvas.edgePaths.keys()]).toEqual(edges.map((edge) => edge.id));
    for (const p of canvas.edgePaths.values()) {
      expect(p.d).not.toMatch(/NaN|Infinity|undefined/);
      expect(p.bounds.minX).toBeGreaterThanOrEqual(0);
      expect(p.bounds.minY).toBeGreaterThanOrEqual(0);
      expect(p.bounds.maxX).toBeLessThan(canvas.width);
      expect(p.bounds.maxY).toBeLessThan(canvas.height);
    }
    expect({ nodes, edges }).toEqual(original);
  });

  it("handles an empty graph and ignores dangling connectors", () => {
    expect(buildWorkflowCanvas([], []).width).toBe(660);
    expect(buildWorkflowCanvas([], []).height).toBe(570);
    const canvas = buildWorkflowCanvas([missing("a")], [{ id: "bad", from: "a", to: "absent" }]);
    expect(canvas.nodes[0].x).toBe(40);
    expect(canvas.nodes[0].y).toBe(40);
    expect(canvas.edgePaths.size).toBe(0);
  });
});

describe("workflow connector ports", () => {
  it("enters a rightward target from its left and a backward target from its right", () => {
    const left = node("left", 40, 40);
    const right = node("right", 400, 80);
    const forward = createWorkflowEdgePath(left, right);
    expect(forward.start).toEqual({ x: 40 + WORKFLOW_NODE_WIDTH, y: 40 + WORKFLOW_NODE_HEIGHT / 2 });
    expect(forward.end).toEqual({ x: 400, y: 80 + WORKFLOW_NODE_HEIGHT / 2 });
    const backward = createWorkflowEdgePath(right, left);
    expect(backward.start.x).toBe(400);
    expect(backward.end.x).toBe(40 + WORKFLOW_NODE_WIDTH);
  });

  it("uses top-to-bottom ports for upward connections and bottom-to-top for downward ones", () => {
    const top = node("top", 40, 40);
    const bottom = node("bottom", 40, 240);
    const down = createWorkflowEdgePath(top, bottom);
    expect(down.start.y).toBe(40 + WORKFLOW_NODE_HEIGHT);
    expect(down.end.y).toBe(240);
    const up = createWorkflowEdgePath(bottom, top);
    expect(up.start.y).toBe(240);
    expect(up.end.y).toBe(40 + WORKFLOW_NODE_HEIGHT);
    expect(up.bounds.minY).toBe(40 + WORKFLOW_NODE_HEIGHT);
    expect(up.bounds.maxY).toBe(240);
  });

  it("keeps all parallel connectors distinct instead of merging the fourth and later", () => {
    const from = node("a", 40, 40);
    const to = node("b", 400, 40);
    const paths = Array.from({ length: 6 }, (_, parallelIndex) =>
      createWorkflowEdgePath(from, to, { parallelIndex, parallelCount: 6 }).d);
    expect(new Set(paths).size).toBe(6);
  });

  it("draws self loops outside the card and keeps duplicate loops distinct", () => {
    const from = node("a", 40, 40);
    const first = createWorkflowEdgePath(from, from, { parallelIndex: 0, parallelCount: 2 });
    const second = createWorkflowEdgePath(from, from, { parallelIndex: 1, parallelCount: 2 });
    expect(first.d).not.toBe(second.d);
    expect(first.bounds.maxX).toBeGreaterThan(from.x + WORKFLOW_NODE_WIDTH);
    expect(first.bounds.minY).toBeLessThan(from.y);
    expect(first.start).not.toEqual(first.end);
  });
});
