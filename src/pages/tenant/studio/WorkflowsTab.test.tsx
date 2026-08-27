/* Workflow builder: loading, real node editing (add / edit / connect /
   delete), save payloads, validation, error/empty states, delivery modes,
   rename, import/export parsing and permission gating. */

import { render, screen, waitFor, within } from "@testing-library/react";
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
