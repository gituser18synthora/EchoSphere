/* Testing tab: text turns go through the REAL backend chat tester
   (router + workflow engine) and the execution trace shows the workflow
   node path, slots and progress state. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TestingTab from "@/pages/tenant/studio/TestingTab";
import { formatChatTime, nowWithMicroseconds } from "@/services/chatTime";
import * as api from "@/services/api";
import type { VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  listScenarios: vi.fn(),
  listPrompts: vi.fn(),
  runSuite: vi.fn(),
  testBotChat: vi.fn(),
  simulateTurn: vi.fn(),
  createVoiceSession: vi.fn(),
  getChannel: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), user: { tenantId: "tn_x" }, hasPermission: () => true }),
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
  language: "hi-IN",
  latencyMs: 24,
  at: "2026-08-05T07:15:30.123456Z",
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

const HANDOFF_TURN = {
  ...TURN_1,
  reply: "I could not verify the details. Please stay on the line while I transfer you.",
  done: true,
  activeWorkflow: null,
  workflow: {
    ...TURN_1.workflow, status: "handoff", nodeTrace: ["n_verify", "n_transfer"],
    slots: { customer_verified: false }, done: true,
  },
};

describe("TestingTab — real runtime chat testing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listScenarios).mockResolvedValue([]);
    vi.mocked(api.listPrompts).mockResolvedValue([]);
    vi.mocked(api.getChannel).mockResolvedValue({
      id: "ch_1", type: "voice", botId: "bot_x", status: "configured", enabled: true,
      detail: "+918047133651 · freeswitch", workflow: "—", lastTest: null,
      config: { phoneNumber: "+918047133651", telephonyProvider: "freeswitch" },
    } as never);
    vi.mocked(api.testBotChat)
      .mockResolvedValueOnce(TURN_1 as never)
      .mockResolvedValueOnce(TURN_2 as never);
  });

  async function sendMessage(user: ReturnType<typeof userEvent.setup>, text: string) {
    // The phone view is the default — text turns happen in the console.
    await user.click(screen.getByRole("button", { name: "Test console" }));
    await user.type(screen.getByPlaceholderText(/I need to see a doctor/), text);
    await user.click(screen.getByRole("button", { name: "Send" }));
  }

  it("runs turns through the backend tester and keeps the session id", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    await sendMessage(user, "i need a plan");
    await screen.findByText(/How much can you pay per month/);
    expect(api.testBotChat).toHaveBeenCalledWith(
      "bot_x",
      "i need a plan",
      undefined,
      expect.arrayContaining([
        expect.objectContaining({ role: "assistant", content: expect.any(String) }),
      ]),
      undefined,
    );

    await sendMessage(user, "2500");
    await screen.findByText(/Goodbye!/);
    // The second turn reuses the session id from the first response.
    expect(api.testBotChat).toHaveBeenLastCalledWith(
      "bot_x",
      "2500",
      "ct_abc123",
      expect.arrayContaining([
        expect.objectContaining({ role: "user", content: "i need a plan" }),
        expect.objectContaining({ role: "assistant", content: TURN_1.reply }),
      ]),
      "hi-IN",
    );
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

  it("ends the text test after handoff until the user resets it", async () => {
    vi.mocked(api.testBotChat).mockReset().mockResolvedValue(HANDOFF_TURN as never);
    const user = userEvent.setup();
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    await sendMessage(user, "wrong verification detail");
    await screen.findByText(/stay on the line while I transfer/i);

    const input = screen.getByLabelText("Simulator input");
    expect(input).toBeDisabled();
    expect(input).toHaveAttribute("placeholder", expect.stringMatching(/select Reset/i));

    await user.click(screen.getAllByRole("button", { name: "Reset" })[0]);
    expect(input).not.toBeDisabled();
  });

  it("renders the published greeting instead of a newer draft", async () => {
    vi.mocked(api.listPrompts).mockResolvedValue([{
      id: "pr_greeting", type: "greeting", activeVersion: 2, publishedVersion: 1,
      versions: [
        { version: 2, variants: [{ language: "en-IN", content: "Draft hello" }] },
        { version: 1, variants: [{ language: "en-IN", content: "Published hello" }] },
      ],
    }] as never);

    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    expect(await screen.findByText("Published hello")).toBeInTheDocument();
    expect(screen.queryByText("Draft hello")).not.toBeInTheDocument();
  });

  it("opens on the phone view by default, showing the channel's configured number and no debug console", async () => {
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    // No click — the phone view is the landing view of the Testing tab.
    expect(screen.getByRole("button", { name: "Phone view" })).toHaveAttribute("aria-pressed", "true");
    const number = await screen.findByTestId("phone-call-number");
    expect(number).toHaveTextContent("+918047133651");
    expect(api.getChannel).toHaveBeenCalledWith("bot_x", "voice");
    expect(screen.getByText("via FreeSWITCH")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start call" })).toBeInTheDocument();
    // The test console (with its trace/debug surfaces) is hidden, not unmounted.
    expect(screen.getByText("Execution trace")).not.toBeVisible();
    expect(screen.getByText("Regression suite")).not.toBeVisible();
  });

  it("phone view degrades to a no-number label when the channel is unavailable", async () => {
    vi.mocked(api.getChannel).mockRejectedValue(new Error("Forbidden"));
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    const number = await screen.findByTestId("phone-call-number");
    expect(number).toHaveTextContent("No voice number assigned");
  });

  it("switching between console and phone view keeps the chat session intact", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    await sendMessage(user, "i need a plan");
    await screen.findByText(/How much can you pay per month/);

    await user.click(screen.getByRole("button", { name: "Phone view" }));
    expect(screen.getByTestId("phone-call-view")).toBeVisible();
    // The phone view never shows the transcript — it lives only in the console.
    expect(screen.getByText(/How much can you pay per month/)).not.toBeVisible();
    expect(screen.getByTestId("phone-call-view")).not.toHaveTextContent(/How much can you pay/);
    await user.click(screen.getByRole("button", { name: "Test console" }));

    // The transcript survived the round trip…
    expect(screen.getByText(/How much can you pay per month/)).toBeVisible();
    // …and the next turn still reuses the same backend session id.
    await sendMessage(user, "2500");
    await screen.findByText(/Goodbye!/);
    expect(api.testBotChat).toHaveBeenLastCalledWith(
      "bot_x", "2500", "ct_abc123", expect.anything(), "hi-IN",
    );
  });

  it("shows an MM:SS.xx timestamp on every simulator message", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><TestingTab bot={BOT} /></MemoryRouter>);

    await sendMessage(user, "i need a plan");
    await screen.findByText(/How much can you pay/);

    const timestamps = screen.getAllByTestId("message-timestamp");
    expect(timestamps).toHaveLength(3);
    for (const timestamp of timestamps) {
      expect(timestamp).toHaveTextContent(/^\d{2}:\d{2}\.\d{2}$/);
      expect(timestamp.getAttribute("dateTime")).toMatch(
        /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/,
      );
    }
  });
});

describe("high-resolution chat timestamps", () => {
  it("creates distinct ISO timestamps and shows two fractional digits", () => {
    const first = nowWithMicroseconds();
    const second = nowWithMicroseconds();

    expect(first).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/);
    expect(second).not.toBe(first);
    expect(formatChatTime("2026-08-05T12:34:56.123456Z")).toMatch(
      /^\d{2}:\d{2}\.12$/,
    );
  });
});
