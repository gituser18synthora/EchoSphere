import type { WorkflowEdge, WorkflowNode } from "@/types/domain";

/** These are also the rendered node dimensions: connectors must use the same
 * footprint as the cards, including for old workflows with saved positions. */
export const WORKFLOW_NODE_WIDTH = 178;
export const WORKFLOW_NODE_HEIGHT = 54;
export const WORKFLOW_CANVAS_PADDING = 40;

type Point = { x: number; y: number };
type Bounds = { minX: number; minY: number; maxX: number; maxY: number };

export interface WorkflowEdgePath {
  d: string;
  lx: number;
  ly: number;
  start: Point;
  end: Point;
  bounds: Bounds;
}

export interface WorkflowCanvas {
  /** Display positions only. The input nodes and their authored fields are
   * untouched; subtract offsetX/offsetY when committing a dragged position. */
  nodes: WorkflowNode[];
  nodeById: Map<string, WorkflowNode>;
  edgePaths: Map<string, WorkflowEdgePath>;
  offsetX: number;
  offsetY: number;
  width: number;
  height: number;
}

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

function path(start: Point, c1: Point, c2: Point, end: Point): WorkflowEdgePath {
  return {
    d: `M${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`,
    // The cubic midpoint places the label on the actual curve.
    lx: (start.x + 3 * c1.x + 3 * c2.x + end.x) / 8,
    ly: (start.y + 3 * c1.y + 3 * c2.y + end.y) / 8 - 6,
    start,
    end,
    // Control-point bounds enclose the entire cubic and its arrowhead.
    bounds: {
      minX: Math.min(start.x, c1.x, c2.x, end.x),
      minY: Math.min(start.y, c1.y, c2.y, end.y),
      maxX: Math.max(start.x, c1.x, c2.x, end.x),
      maxY: Math.max(start.y, c1.y, c2.y, end.y),
    },
  };
}

/** Route towards the facing side of a card. In particular an upward edge
 * leaves the source top and reaches the target bottom; it must not travel
 * through both cards as a downward bottom-to-top curve would. */
export function createWorkflowEdgePath(
  from: WorkflowNode,
  to: WorkflowNode,
  { parallelIndex = 0, parallelCount = 1 }: { parallelIndex?: number; parallelCount?: number } = {},
): WorkflowEdgePath {
  const w = WORKFLOW_NODE_WIDTH;
  const h = WORKFLOW_NODE_HEIGHT;
  const count = Math.max(1, parallelCount);
  const index = Math.max(0, Math.min(parallelIndex, count - 1));
  const spread = count > 1 ? (index + 1) / (count + 1) - 0.5 : 0;
  const portY = spread * h * 0.9;
  const portX = spread * w * 0.8;

  if (from.id === to.id) {
    const extent = 56 + index * 18;
    return path(
      { x: from.x + w, y: from.y + h * 0.7 },
      { x: from.x + w + extent, y: from.y + h * 0.7 },
      { x: from.x + w + extent, y: from.y - extent },
      { x: from.x + w * 0.7, y: from.y },
    );
  }

  if (to.x >= from.x + w + 16) {
    const start = { x: from.x + w, y: from.y + h / 2 + portY };
    const end = { x: to.x, y: to.y + h / 2 + portY };
    const midX = (start.x + end.x) / 2;
    return path(start, { x: midX, y: start.y }, { x: midX, y: end.y }, end);
  }
  if (to.x + w <= from.x - 16) {
    const start = { x: from.x, y: from.y + h / 2 + portY };
    const end = { x: to.x + w, y: to.y + h / 2 + portY };
    const midX = (start.x + end.x) / 2;
    return path(start, { x: midX, y: start.y }, { x: midX, y: end.y }, end);
  }
  if (to.y >= from.y + h + 16) {
    const start = { x: from.x + w / 2 + portX, y: from.y + h };
    const end = { x: to.x + w / 2 + portX, y: to.y };
    const midY = (start.y + end.y) / 2;
    return path(start, { x: start.x, y: midY }, { x: end.x, y: midY }, end);
  }
  if (to.y + h <= from.y - 16) {
    const start = { x: from.x + w / 2 + portX, y: from.y };
    const end = { x: to.x + w / 2 + portX, y: to.y + h };
    const midY = (start.y + end.y) / 2;
    return path(start, { x: start.x, y: midY }, { x: end.x, y: midY }, end);
  }

  // Overlapping/adjacent authored cards cannot be untangled without moving
  // them. Route outside their shared rectangle so the edge remains visible.
  const right = to.x >= from.x;
  const start = { x: from.x + (right ? w : 0), y: from.y + h / 2 + portY };
  const end = { x: to.x + (right ? w : 0), y: to.y + h / 2 - portY };
  const lane = right
    ? Math.max(from.x, to.x) + w + 56 + index * 18
    : Math.min(from.x, to.x) - 56 - index * 18;
  // Equal-y endpoints need different control heights to produce a visible
  // loop rather than a straight line which doubles back over itself.
  const bend = Math.abs(start.y - end.y) < 1 ? 36 + index * 10 : 0;
  return path(start, { x: lane, y: start.y - bend }, { x: lane, y: end.y + bend }, end);
}

/** Resolve legacy documents for presentation without adding/replacing any
 * saved coordinates. Some configuration repairs created nodes without x/y;
 * one undefined value used to turn the complete SVG dimensions into NaN. */
export function buildWorkflowCanvas(
  authoredNodes: WorkflowNode[],
  edges: WorkflowEdge[],
  stableOffset?: { offsetX: number; offsetY: number },
  fallbackPositions?: ReadonlyMap<string, { x: number; y: number }>,
): WorkflowCanvas {
  const w = WORKFLOW_NODE_WIDTH;
  const h = WORKFLOW_NODE_HEIGHT;
  const gap = 36;
  const valid = authoredNodes.filter((n) => finite(n.x) && finite(n.y));
  // Retain the initial display anchors for unpositioned nodes. Otherwise
  // dragging one of them creates a new authored maximum x and sends all the
  // remaining fallback nodes farther right on the next render.
  const retained = new Map<string, Point>();
  for (const node of authoredNodes) {
    if (finite(node.x) && finite(node.y)) continue;
    const fallback = fallbackPositions?.get(node.id);
    const x = finite(node.x) ? node.x : fallback?.x;
    const y = finite(node.y) ? node.y : fallback?.y;
    if (finite(x) && finite(y)) retained.set(node.id, { x, y });
  }
  // Reserve every retained position before placing new fallback nodes,
  // regardless of their order in the authored document.
  const occupied = [...valid.map((n) => ({ x: n.x, y: n.y })), ...retained.values()];
  const minX = valid.length ? Math.min(...valid.map((n) => n.x)) : WORKFLOW_CANVAS_PADDING;
  const minY = valid.length ? Math.min(...valid.map((n) => n.y)) : WORKFLOW_CANVAS_PADDING;
  const fallbackX = valid.length ? Math.max(...valid.map((n) => n.x)) + w + gap * 2 : minX;
  let fallbackRow = 0;
  const collides = (x: number, y: number) => occupied.some((p) =>
    x < p.x + w + gap && x + w + gap > p.x && y < p.y + h + gap && y + h + gap > p.y);

  const rawNodes = authoredNodes.map((node) => {
    if (finite(node.x) && finite(node.y)) return { ...node };
    const anchored = retained.get(node.id);
    if (anchored) return { ...node, ...anchored };
    const fallback = fallbackPositions?.get(node.id);
    const knownX = finite(node.x) ? node.x : finite(fallback?.x) ? fallback.x : undefined;
    const knownY = finite(node.y) ? node.y : finite(fallback?.y) ? fallback.y : undefined;
    let x = knownX ?? (finite(knownY) ? minX : fallbackX);
    let y = knownY ?? (finite(knownX) ? minY : minY + fallbackRow++ * (h + gap));
    while (collides(x, y)) {
      if (finite(knownY)) x += w + gap;
      else y += h + gap;
    }
    occupied.push({ x, y });
    return { ...node, x, y };
  });
  const rawById = new Map(rawNodes.map((node) => [node.id, node]));
  const counts = new Map<string, number>();
  for (const edge of edges) {
    const key = `${edge.from}\u0000${edge.to}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const indexes = new Map<string, number>();
  const parallel = new Map<string, { parallelIndex: number; parallelCount: number }>();
  let extentMinX = Math.min(0, ...rawNodes.map((n) => n.x));
  let extentMinY = Math.min(0, ...rawNodes.map((n) => n.y));
  let extentMaxX = Math.max(0, ...rawNodes.map((n) => n.x + w));
  let extentMaxY = Math.max(0, ...rawNodes.map((n) => n.y + h));
  for (const edge of edges) {
    const from = rawById.get(edge.from);
    const to = rawById.get(edge.to);
    if (!from || !to) continue;
    const key = `${edge.from}\u0000${edge.to}`;
    const options = { parallelIndex: indexes.get(key) ?? 0, parallelCount: counts.get(key) ?? 1 };
    indexes.set(key, options.parallelIndex + 1);
    parallel.set(edge.id, options);
    const { bounds } = createWorkflowEdgePath(from, to, options);
    extentMinX = Math.min(extentMinX, bounds.minX);
    extentMinY = Math.min(extentMinY, bounds.minY);
    extentMaxX = Math.max(extentMaxX, bounds.maxX);
    extentMaxY = Math.max(extentMaxY, bounds.maxY);
  }

  const offsetX = finite(stableOffset?.offsetX) ? stableOffset.offsetX : extentMinX < 0 ? WORKFLOW_CANVAS_PADDING - extentMinX : 0;
  const offsetY = finite(stableOffset?.offsetY) ? stableOffset.offsetY : extentMinY < 0 ? WORKFLOW_CANVAS_PADDING - extentMinY : 0;
  const nodes = rawNodes.map((node) => ({ ...node, x: node.x + offsetX, y: node.y + offsetY }));
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const edgePaths = new Map<string, WorkflowEdgePath>();
  for (const edge of edges) {
    const from = nodeById.get(edge.from);
    const to = nodeById.get(edge.to);
    if (from && to) edgePaths.set(edge.id, createWorkflowEdgePath(from, to, parallel.get(edge.id)));
  }
  return {
    nodes, nodeById, edgePaths, offsetX, offsetY,
    width: Math.max(660, Math.ceil(extentMaxX + offsetX + WORKFLOW_CANVAS_PADDING)),
    height: Math.max(570, Math.ceil(extentMaxY + offsetY + WORKFLOW_CANVAS_PADDING)),
  };
}
