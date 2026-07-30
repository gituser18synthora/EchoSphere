/* Testing tab: text turns go through the REAL backend chat tester
   (router + workflow engine) and the execution trace shows the workflow
   node path, slots and progress state. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TestingTab from "@/pages/tenant/studio/TestingTab";
import * as api from "@/services/api";
import type { VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  listScenarios: vi.fn(),
  listPrompts: vi.fn(),
  runSuite: vi.fn(),
  testBotChat: vi.fn(),
  createVoiceSession: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), user: { tenantId: "tn_x" } }),
}));

const BOT = { id: "bot_x", name: "Collections Bot", version: "v0.3.0" } as VoiceBot;

const TURN_1 = {
  sessionId: "ct_abc123",
  route: "workflow",
  action: "collections_plan",
  matchedIntent: "setup_plan",
  confidence: 0.9,
  reason: "intent_workflow",
  reply: "I can set up a payment plan. How much can you pay per month?",
  done: false,
  activeWorkflow: "collections_plan",
  workflow: {
    name: "collections_plan", source: "definition" as const, status: "collecting",
    workflowId: "wf_1", nodeTrace: ["n1", "n2", "n3"], slots: {}, done: false,
  },
};

const TURN_2 = {
  ...TURN_1,
  reply: "Your plan is registered. Goodbye!",
  done: true,
  activeWorkflow: null,
  workflow: {
    ...TURN_1.workflow, status: "done", nodeTrace: ["n3", "n4", "n5"],
    slots: { amount: "2500" }, done: true,
  },
};

describe("TestingTab — real runtime chat testing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listScenarios).mockResolvedValue([]);
    vi.mocked(api.listPrompts).mockResolvedValue([]);
    vi.mocked(api.testBotChat)
      .mockResolvedValueOnce(TURN_1 as never)
      .mockResolvedValueOnce(TURN_2 as never);
  });

  async function sendMessage(user: ReturnType<typeof userEvent.setup>, text: string) {
    await user.type(screen.getByPlaceholderText(/I need to see a doctor/), text);
    await user.click(screen.getByRole("button", { name: "Send" }));
  }

  it("runs turns through the backend tester and keeps the session id", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    await sendMessage(user, "i need a plan");
    await screen.findByText(/How much can you pay per month/);
    expect(api.testBotChat).toHaveBeenCalledWith("bot_x", "i need a plan", undefined);

    await sendMessage(user, "2500");
    await screen.findByText(/Goodbye!/);
    // The second turn reuses the session id from the first response.
    expect(api.testBotChat).toHaveBeenLastCalledWith("bot_x", "2500", "ct_abc123");
  });

  it("shows the executed workflow nodes and collected slots in the trace", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    await sendMessage(user, "i need a plan");
    await screen.findByText(/How much can you pay/);
    expect(screen.getByTestId("workflow-node-trace")).toHaveTextContent("n1 → n2 → n3");
    expect(screen.getByText("in progress")).toBeInTheDocument();

    await sendMessage(user, "2500");
    await screen.findByText(/Goodbye!/);
    expect(screen.getByTestId("workflow-node-trace")).toHaveTextContent("n3 → n4 → n5");
    expect(screen.getByTestId("workflow-slots")).toHaveTextContent("amount=2500");
    expect(screen.getByText("finished")).toBeInTheDocument();
  });

  it("surfaces a failed turn without crashing the transcript", async () => {
    vi.mocked(api.testBotChat).mockReset().mockRejectedValue(new Error("API down"));
    const user = userEvent.setup();
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    await sendMessage(user, "hello");
    await screen.findByText(/test turn failed/i);
  });
});
