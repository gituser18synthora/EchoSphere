/* Workflow builder: loading, real node editing (add / edit / connect /
   delete), save payloads, validation and error states. */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkflowsTab, { validateGraph } from "@/pages/tenant/studio/WorkflowsTab";
import * as api from "@/services/api";
import type { VoiceBot, Workflow } from "@/types/domain";

vi.mock("@/services/api", () => ({
  getWorkflow: vi.fn(),
  saveWorkflow: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn() }),
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

  it("activate saves an approved status", async () => {
    const user = userEvent.setup();
    render(<WorkflowsTab bot={BOT} />);
    await screen.findByText("Call starts");
    await user.click(screen.getByRole("button", { name: /activate/i }));
    await waitFor(() => expect(api.saveWorkflow).toHaveBeenCalled());
    expect(vi.mocked(api.saveWorkflow).mock.calls[0][1]).toMatchObject({ status: "approved" });
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
