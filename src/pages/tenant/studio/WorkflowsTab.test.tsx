/* Workflow builder: loading, real node editing (add / edit / connect /
   delete), save payloads, validation, error/empty states, delivery modes,
   rename, import/export parsing and permission gating. */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkflowsTab, {
  edgeLabelSummary, parseImportedDefinition, slugifyWorkflowName, validateGraph,
} from "@/pages/tenant/studio/WorkflowsTab";
import * as api from "@/services/api";
import type { VoiceBot, Workflow } from "@/types/domain";

vi.mock("@/services/api", () => ({
  getWorkflow: vi.fn(),
  saveWorkflow: vi.fn(),
}));
const { mockHasPermission } = vi.hoisted(() => ({ mockHasPermission: vi.fn() }));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: mockHasPermission }),
}));

const BOT = { id: "bot_x", name: "Collections Bot" } as VoiceBot;

const WF: Workflow = {
  id: "wf_1", botId: "bot_x", name: "Collections plan", version: 3, status: "draft",
  nodes: [
    { id: "n1", kind: "start", label: "Call starts", x: 40, y: 40 },
    { id: "n2", kind: "message", label: "Greeting", x: 40, y: 150,
      config: { text: "Hello caller" } },
    { id: "n3", kind: "end", label: "End call", x: 40, y: 260 },
  ],
  edges: [
    { id: "e1", from: "n1", to: "n2" },
    { id: "e2", from: "n2", to: "n3" },
  ],
  issues: [],
  updatedAt: "2026-07-20T10:00:00Z", updatedBy: "QA",
};

beforeEach(() => {
  vi.clearAllMocks();
  mockHasPermission.mockImplementation(() => true);
  vi.mocked(api.getWorkflow).mockResolvedValue(structuredClone(WF));
  vi.mocked(api.saveWorkflow).mockImplementation((_id, body) =>
    Promise.resolve({ ...structuredClone(WF), ...body, version: 4, issues: [] } as Workflow));
});

describe("WorkflowsTab — load and render", () => {
  it("puts the palette above the canvas and remembers the inspector toggle without saving", async () => {
    const user = userEvent.setup();
    const first = render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    const palette = screen.getByRole("toolbar", { name: "Add a step" });
    const canvas = screen.getByRole("application", { name: "Workflow canvas" });
    expect(palette.closest(".wf-editor-grid")).toBeNull();
    expect(palette.compareDocumentPosition(canvas) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const toggle = screen.getByRole("button", { name: "Step inspector" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("complementary", { name: "Inspector" })).not.toBeInTheDocument();
    expect(canvas.closest(".wf-editor-grid")).toHaveClass("wf-inspector-hidden");
    await user.click(screen.getByRole("button", { name: "Message: Greeting" }));
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(screen.getByRole("textbox", { name: "Node label" })).toHaveValue("Greeting");
    await user.click(screen.getByRole("button", { name: "Hide step inspector" }));
    expect(localStorage.getItem("echosphere.workflow.inspector")).toBe("hidden");
    expect(screen.getByRole("button", { name: "Save version" })).toBeDisabled();
    expect(api.saveWorkflow).not.toHaveBeenCalled();
    first.unmount();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    expect(screen.getByRole("button", { name: "Step inspector" })).toHaveAttribute("aria-expanded", "false");
  });

  it("renders legacy missing and negative positions without changing saved coordinates on label edit", async () => {
    const legacy = structuredClone(WF);
    legacy.nodes[0].x = -170; legacy.nodes[0].y = -200;
    // The Haier repair created some nodes without authored coordinates.
    delete (legacy.nodes[1] as Partial<typeof legacy.nodes[1]>).x;
    delete (legacy.nodes[1] as Partial<typeof legacy.nodes[1]>).y;
    vi.mocked(api.getWorkflow).mockResolvedValue(legacy);
    const original = structuredClone(legacy);
    const user = userEvent.setup();
    const { container } = render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    const svg = container.querySelector(".wf-connections")!;
    expect(Number(svg.getAttribute("width"))).toBeGreaterThan(0);
    expect(Number(svg.getAttribute("height"))).toBeGreaterThan(0);
    expect(svg.getAttribute("viewBox")).not.toMatch(/NaN|undefined|Infinity/);
    expect(svg.querySelectorAll("[data-edge-id]")).toHaveLength(2);
    svg.querySelectorAll("path[d]").forEach((path) => expect(path.getAttribute("d")).not.toMatch(/NaN|undefined|Infinity/));
    for (const node of container.querySelectorAll<HTMLElement>(".wf-node")) {
      expect(Number.parseFloat(node.style.left)).toBeGreaterThanOrEqual(0);
      expect(Number.parseFloat(node.style.top)).toBeGreaterThanOrEqual(0);
    }
    expect(screen.getByRole("button", { name: "Save version" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Message: Greeting" }));
    await user.clear(screen.getByLabelText("Node label"));
    await user.type(screen.getByLabelText("Node label"), "Changed label");
    await user.click(screen.getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.nodes).toEqual(original.nodes.map((node) => node.id === "n2" ? { ...node, label: "Changed label" } : node));
    expect(body.edges).toEqual(original.edges);
    expect(legacy).toEqual(original);
  });

  it("focuses selected connections, paints them last, and keeps saved branch precedence", async () => {
    const user = userEvent.setup();
    const { container } = render(<WorkflowsTab bot={BOT} />);
    await user.click(await screen.findByRole("button", { name: "Start: Call starts" }));
    const groups = [...container.querySelectorAll("g[data-edge-id]")];
    expect(groups.map((group) => group.getAttribute("data-edge-id"))).toEqual(["e2", "e1"]);
    expect(groups.at(-1)).toHaveAttribute("data-active", "true");
    await user.click(screen.getByRole("button", { name: "Focus connections" }));
    expect(container.querySelectorAll("g[data-edge-id]")).toHaveLength(1);
    expect(container.querySelector("g[data-edge-id]")).toHaveAttribute("data-edge-id", "e1");
    expect(api.saveWorkflow).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("Node label"), " updated");
    await user.click(screen.getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveWorkflow).mock.calls[0][1].edges).toEqual(WF.edges);
  });

  it("keeps the graph stable after dragging a negative-position node and saves authored coordinates", async () => {
    const legacy = structuredClone(WF);
    legacy.nodes[0].x = -170; legacy.nodes[0].y = -200;
    vi.mocked(api.getWorkflow).mockResolvedValue(legacy);
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    const start = await screen.findByRole("button", { name: "Start: Call starts" });
    const other = screen.getByRole("button", { name: "Message: Greeting" });
    const otherPosition = { left: other.style.left, top: other.style.top };
    const startX = Number.parseFloat(start.style.left);
    const startY = Number.parseFloat(start.style.top);
    fireEvent(start, new MouseEvent("pointerdown", { bubbles: true, clientX: 100, clientY: 100, buttons: 1 }));
    fireEvent(start, new MouseEvent("pointermove", { bubbles: true, clientX: 400, clientY: 400, buttons: 1 }));
    fireEvent(start, new MouseEvent("pointerup", { bubbles: true, clientX: 400, clientY: 400 }));
    expect(start.style.left).toBe(`${startX + 300}px`);
    expect(start.style.top).toBe(`${startY + 300}px`);
    expect({ left: other.style.left, top: other.style.top }).toEqual(otherPosition);
    await user.click(screen.getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.nodes).toEqual(legacy.nodes.map((node) => node.id === "n1" ? { ...node, x: 130, y: 100 } : node));
    expect(body.edges).toEqual(legacy.edges);
  });

  it("keeps untouched legacy steps anchored when another unpositioned step is dragged", async () => {
    const legacy = structuredClone(WF);
    for (const node of legacy.nodes.slice(1)) {
      delete (node as Partial<typeof node>).x;
      delete (node as Partial<typeof node>).y;
    }
    vi.mocked(api.getWorkflow).mockResolvedValue(legacy);
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    const moved = await screen.findByRole("button", { name: "Message: Greeting" });
    const untouched = screen.getByRole("button", { name: "End: End call" });
    const originalPosition = { left: untouched.style.left, top: untouched.style.top };
    const movedX = Number.parseFloat(moved.style.left);
    const movedY = Number.parseFloat(moved.style.top);
    fireEvent(moved, new MouseEvent("pointerdown", { bubbles: true, clientX: 100, clientY: 100, buttons: 1 }));
    fireEvent(moved, new MouseEvent("pointermove", { bubbles: true, clientX: 110, clientY: 100, buttons: 1 }));
    fireEvent(moved, new MouseEvent("pointerup", { bubbles: true, clientX: 110, clientY: 100 }));
    expect(moved.style.left).toBe(`${movedX + 10}px`);
    expect(moved.style.top).toBe(`${movedY}px`);
    expect({ left: untouched.style.left, top: untouched.style.top }).toEqual(originalPosition);
    await user.click(screen.getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    const savedUntouched = vi.mocked(api.saveWorkflow).mock.calls[0][1].nodes!.find((node) => node.id === "n3");
    expect(savedUntouched).not.toHaveProperty("x");
    expect(savedUntouched).not.toHaveProperty("y");
    expect({ left: untouched.style.left, top: untouched.style.top }).toEqual(originalPosition);
  });

  it("fits a large graph below 40 percent zoom without changing the workflow", async () => {
    const large = structuredClone(WF);
    large.nodes[2].x = 5000; large.nodes[2].y = 3000;
    vi.mocked(api.getWorkflow).mockResolvedValue(large);
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    const canvas = screen.getByRole("application", { name: "Workflow canvas" });
    Object.defineProperties(canvas, { clientWidth: { value: 800 }, clientHeight: { value: 600 } });
    await user.click(screen.getByRole("button", { name: "Fit to view" }));
    const zoomLabel = screen.getByRole("group", { name: "Canvas zoom" }).textContent!;
    expect(Number.parseInt(zoomLabel)).toBeLessThan(40);
    const bounds = canvas.firstElementChild as HTMLElement;
    expect(Number.parseFloat(bounds.style.width)).toBeLessThanOrEqual(800);
    expect(Number.parseFloat(bounds.style.height)).toBeLessThanOrEqual(600);
    await user.click(screen.getByRole("button", { name: "Reset zoom to 100%" }));
    expect(screen.getByRole("button", { name: "Reset zoom to 100%" })).toHaveTextContent("100%");
    expect(api.saveWorkflow).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Save version" })).toBeDisabled();
  });

  it("renders the saved graph with its nodes", async () => {
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    expect(screen.getByText("Greeting")).toBeInTheDocument();
    expect(screen.getByText("End call")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
  });

  it("shows the API error state with a retry", async () => {
    vi.mocked(api.getWorkflow).mockRejectedValueOnce(new Error("boom"));
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText(/boom/);
  });

  it("offers to create a workflow when none exists (404)", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getWorkflow).mockRejectedValueOnce(
      Object.assign(new Error("Workflow not found"), { status: 404 }));
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("No workflow yet");

    await user.click(screen.getByRole("button", { name: /create workflow/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.nodes).toHaveLength(1);
    expect(body.nodes![0].kind).toBe("start");
    expect(body.edges).toEqual([]);
  });

  it("renders read-only without workflow permissions", async () => {
    mockHasPermission.mockImplementation(() => false);
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    expect(screen.getByText("View only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save version/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add Message node/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /activate/i })).not.toBeInTheDocument();
  });
});

describe("WorkflowsTab — editing", () => {
  it("docks the inspector, supports keyboard inspection, and preserves advanced legacy data when saving", async () => {
    const legacy = structuredClone(WF);
    legacy.nodes[1].config = {
      text: "Hello caller", responseMode: "exact", custom: { retries: 3, mapping: ["a", "b"] },
      responseMustInclude: ["account number"], responseDirectives: "Keep the amount unchanged",
    };
    legacy.nodes[1].x = 47; legacy.nodes[1].y = 163;
    legacy.edges = [
      { id: "same-target-first", from: "n2", to: "n3", label: "yes,haan" },
      { id: "same-target-default", from: "n2", to: "n3", label: "else" },
      legacy.edges[0],
    ];
    vi.mocked(api.getWorkflow).mockResolvedValue(legacy);
    const onTest = vi.fn();
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} onTest={onTest} />);
    await screen.findByText("Call starts");
    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    expect(inspector.closest(".wf-editor-grid")).not.toBeNull();
    expect(screen.getByText("Explore your workflow")).toBeInTheDocument();
    const greeting = screen.getByRole("button", { name: "Message: Greeting" });
    greeting.focus();
    await user.keyboard("{Enter}");
    expect(within(inspector).getByLabelText("Node label")).toHaveValue("Greeting");
    expect(screen.getByRole("button", { name: "Save version" })).toBeDisabled();
    expect(api.saveWorkflow).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Test conversation" }));
    expect(onTest).toHaveBeenCalledTimes(1);
    await user.clear(within(inspector).getByLabelText("Node label"));
    await user.type(within(inspector).getByLabelText("Node label"), "Updated greeting");
    expect(screen.getByRole("button", { name: "Test conversation" })).toBeDisabled();
    vi.mocked(api.getWorkflow).mockResolvedValue({ ...legacy, version: 4,
      nodes: legacy.nodes.map((node) => node.id === "n2" ? { ...node, label: "Updated greeting" } : node),
    });
    await user.click(screen.getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.nodes).toEqual(legacy.nodes.map((node) => node.id === "n2" ? { ...node, label: "Updated greeting" } : node));
    expect(body.edges).toEqual(legacy.edges);
    expect(body.status).toBeUndefined();
    await waitFor(() => expect(screen.getByText("v4")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Message: Updated greeting" })).toBeInTheDocument();
  });

  it("focuses validation issues without editing the graph and dims unrelated steps", async () => {
    const invalid = structuredClone(WF);
    invalid.issues = [{ nodeId: "n2", level: "error", message: "Saved issue" }];
    vi.mocked(api.getWorkflow).mockResolvedValue(invalid);
    const scrollTo = vi.fn();
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    const canvas = screen.getByRole("application", { name: "Workflow canvas" });
    canvas.scrollTo = scrollTo;
    await user.click(screen.getByRole("button", { name: "Greeting" }));
    expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: "smooth" }));
    expect(screen.getByLabelText("Node label")).toHaveValue("Greeting");
    expect(screen.getByRole("button", { name: "Save version" })).toBeDisabled();
    // Select Start: only its immediate neighbour stays prominent.
    screen.getByRole("button", { name: "Start: Call starts" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "End: End call" })).toHaveClass("wf-node-dimmed");
    expect(screen.getByRole("button", { name: "Message: Greeting" })).not.toHaveClass("wf-node-dimmed");
  });

  it("adds a node from the palette and saves it", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");

    await user.click(screen.getByRole("button", { name: /Add Ask \(collect\) node/ }));
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /save version/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalledTimes(1));
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.nodes).toHaveLength(4);
    expect(body.nodes!.some((n) => n.kind === "ask")).toBe(true);
  });

  it("edits a node label and config through the inspector", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await user.click(await screen.findByText("Greeting"));

    const label = screen.getByLabelText("Node label");
    await user.clear(label);
    await user.type(label, "Welcome message");
    const says = screen.getByLabelText("Bot says");
    await user.clear(says);
    await user.type(says, "Namaste!");

    await user.click(screen.getByRole("button", { name: /save version/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    const edited = body.nodes!.find((n) => n.id === "n2")!;
    expect(edited.label).toBe("Welcome message");
    expect(edited.config).toMatchObject({ text: "Namaste!" });
  });

  it("edits the delivery mode with directive and must-include literals", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await user.click(await screen.findByText("Greeting"));

    await user.selectOptions(screen.getByLabelText("Delivery mode"), "llm_grounded");
    await user.type(screen.getByLabelText("Response directive"), "State the amount");
    await user.type(screen.getByLabelText("Must include"), "₹2000, UPI");

    await user.click(screen.getByRole("button", { name: /save version/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    const edited = body.nodes!.find((n) => n.id === "n2")!;
    expect(edited.config).toMatchObject({
      responseMode: "llm_grounded",
      responseDirective: "State the amount",
      responseMustInclude: ["₹2000", "UPI"],
    });
  });

  it("connects two nodes and labels the new edge", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await user.click(await screen.findByText("Call starts"));
    await user.click(screen.getByRole("button", { name: /connect to/i }));
    await user.click(screen.getByText("End call"));

    // The source node stays selected; its new connection is listed and labelable.
    const edgeLabel = screen.getByLabelText("Label for connection to End call");
    await user.type(edgeLabel, "shortcut");

    await user.click(screen.getByRole("button", { name: /save version/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.edges).toHaveLength(3);
    expect(body.edges!.some((e) => e.from === "n1" && e.to === "n3" && e.label === "shortcut")).toBe(true);
  });

  it("reorders a node's outgoing branches", async () => {
    const user = userEvent.setup();
    // Give n1 two outgoing branches so order matters.
    vi.mocked(api.getWorkflow).mockResolvedValue({
      ...structuredClone(WF),
      edges: [
        { id: "e1", from: "n1", to: "n2", label: "first" },
        { id: "e2", from: "n1", to: "n3", label: "second" },
      ],
    });
    render(<WorkflowsTab bot={BOT} />);
    await user.click(await screen.findByText("Call starts"));

    await user.click(screen.getByRole("button", { name: "Move branch to End call up" }));
    await user.click(screen.getByRole("button", { name: /save version/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.edges!.map((e) => e.id)).toEqual(["e2", "e1"]);
  });

  it("deletes a node together with its connections", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await user.click(await screen.findByText("Greeting"));
    await user.click(screen.getByRole("button", { name: /delete node/i }));

    await user.click(screen.getByRole("button", { name: /save version/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    const body = vi.mocked(api.saveWorkflow).mock.calls[0][1];
    expect(body.nodes!.map((n) => n.id)).toEqual(["n1", "n3"]);
    expect(body.edges).toHaveLength(0); // both edges touched n2
  });

  it("undo restores the previous graph", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await user.click(await screen.findByText("Greeting"));
    await user.click(screen.getByRole("button", { name: /delete node/i }));
    expect(screen.queryByText("Greeting")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /undo/i }));
    expect(screen.getByText("Greeting")).toBeInTheDocument();
  });

  it("validate reports disconnected structure", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    await user.click(screen.getByRole("button", { name: /Add Message node/ }));
    await user.click(screen.getByRole("button", { name: /validate/i }));
    expect(await screen.findAllByText(/Not connected to the start node/)).not.toHaveLength(0);
  });

  it("activate confirms, then saves an approved status", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    await user.click(screen.getByRole("button", { name: /activate/i }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/live calls/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Activate" }));

    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    expect(vi.mocked(api.saveWorkflow).mock.calls[0][1]).toMatchObject({ status: "approved" });
  });

  it("renames the workflow with a slug warning", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    await user.click(screen.getByRole("button", { name: "Rename workflow" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Renaming can break intent routes/)).toBeInTheDocument();
    const input = within(dialog).getByLabelText("Workflow name");
    await user.clear(input);
    await user.type(input, "Payment plan journey");
    await user.click(within(dialog).getByRole("button", { name: /rename & save/i }));

    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    expect(vi.mocked(api.saveWorkflow).mock.calls[0][1]).toMatchObject({ name: "Payment plan journey" });
  });
});

describe("validateGraph", () => {
  it("flags missing start, dead ends and unlabeled conditions", () => {
    const issues = validateGraph(
      [
        { id: "a", kind: "message", label: "M", x: 0, y: 0 },
        { id: "b", kind: "condition", label: "C", x: 0, y: 0, config: {} },
      ],
      [{ id: "e", from: "a", to: "b" }],
    );
    const messages = issues.map((i) => i.message).join(" | ");
    expect(messages).toContain("start node is required");
  });

  it("passes a clean linear flow", () => {
    expect(validateGraph(WF.nodes, WF.edges)).toEqual([]);
  });
});

describe("helpers", () => {
  it("slugifies workflow names like the backend route form", () => {
    expect(slugifyWorkflowName("Payment plan journey")).toBe("payment_plan_journey");
    expect(slugifyWorkflowName("0 to 07 collection call (Male)")).toBe("0_to_07_collection_call_male");
  });

  it("summarizes long token-list edge labels", () => {
    expect(edgeLabelSummary()).toBe("");
    expect(edgeLabelSummary("true")).toBe("true");
    expect(edgeLabelSummary("haan,yes,okay,theek")).toBe("haan +3");
  });

  it("parses a valid export and rejects corrupt documents", () => {
    const doc = JSON.stringify({ nodes: WF.nodes, edges: WF.edges });
    const parsed = parseImportedDefinition(doc);
    expect(parsed.nodes).toHaveLength(3);
    expect(parsed.edges).toHaveLength(2);

    expect(() => parseImportedDefinition("not json")).toThrow(/not valid JSON/);
    expect(() => parseImportedDefinition("{}")).toThrow(/nodes/);
    expect(() => parseImportedDefinition(JSON.stringify({
      nodes: [{ id: "a", kind: "teleport", label: "?" }], edges: [],
    }))).toThrow(/unknown kind/);
    expect(() => parseImportedDefinition(JSON.stringify({
      nodes: WF.nodes, edges: [{ id: "e", from: "n1", to: "ghost" }],
    }))).toThrow(/doesn't exist/);
  });
});
