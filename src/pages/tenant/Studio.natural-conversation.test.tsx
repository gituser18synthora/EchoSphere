import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Studio from "./Studio";
import { getBot } from "@/services/api";
import type { VoiceBot } from "@/types/domain";

const toast = vi.fn();
vi.mock("@/services/api", () => ({ getBot: vi.fn() }));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ user: { role: "tenant_admin" }, hasPermission: () => true, toast }),
}));

vi.mock("./studio/NaturalConversationTab", async () => {
  const { useEffect, useState } = await import("react");
  return {
    default: function NaturalConversationEditor({ onDirtyChange, onSavingChange, onNavigate }: {
      onDirtyChange: (dirty: boolean) => void;
      onSavingChange: (saving: boolean) => void;
      onNavigate: (tab: string) => void;
    }) {
      const [value, setValue] = useState("");
      useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
      useEffect(() => () => onSavingChange(false), [onSavingChange]);
      return <>
        <input aria-label="Natural conversation edit" value={value} onChange={(event) => {
          setValue(event.target.value);
          onDirtyChange(true);
        }} />
        <button onClick={() => onSavingChange(true)}>Start saving</button>
        <button onClick={() => {
          onDirtyChange(false);
          onSavingChange(false);
        }}>Finish saving</button>
        <button onClick={() => onNavigate("testing")}>Test saved conversation</button>
      </>;
    },
  };
});
vi.mock("./studio/VoiceTab", () => ({ default: () => <div>Voice editor</div> }));
vi.mock("./studio/TestingTab", () => ({ default: () => <div>Testing panel</div> }));
vi.mock("./studio/PublishTab", () => ({ default: () => <div>Publish panel</div> }));
vi.mock("./studio/OverviewTab", () => ({ default: () => null }));
vi.mock("./studio/KnowledgeTab", () => ({ default: () => null }));
vi.mock("./studio/PromptsTab", () => ({ default: () => null }));
vi.mock("./studio/TurnDetectionTab", () => ({ default: () => null }));
vi.mock("./studio/IntentsTab", () => ({ default: () => null }));
vi.mock("./studio/ApisTab", () => ({ default: () => null }));
vi.mock("./studio/WorkflowsTab", () => ({ default: () => null }));
vi.mock("./studio/ChannelsTab", () => ({ default: () => null }));
vi.mock("./studio/AnalyticsTab", () => ({ default: () => null }));

const BOT: VoiceBot = {
  id: "bot_x", tenantId: "tn_x", name: "Support", useCase: "Support",
  description: "", languages: ["hi-IN"], status: "published", version: "v1.0.0",
  liveVersion: "v1.0.0", owner: "Admin", health: "good", containment: 0,
  callsToday: 0, callsMonth: 0, avgCostPerCall: null, csat: 0, channels: [],
  guardrailProfileId: "", updatedAt: "2026-09-09T00:00:00Z",
  readiness: [{ id: "r2", label: "Voice configured", done: true, studioTab: "voice" }],
};

function mount() {
  render(
    <MemoryRouter initialEntries={["/t/bots/bot_x/natural-conversation"]}>
      <Routes><Route path="/t/bots/:botId/:tab" element={<Studio />} /></Routes>
    </MemoryRouter>,
  );
  return userEvent.setup();
}

describe("Studio Natural Conversation navigation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getBot).mockResolvedValue(BOT);
  });

  it("opens the direct link and switches clean tabs without a discard prompt", async () => {
    const user = mount();
    await screen.findByRole("textbox", { name: "Natural conversation edit" });
    expect(getBot).toHaveBeenCalledWith("bot_x");
    expect(screen.getByRole("tab", { name: "Natural Conversation" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText("Draft saved")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Voice" }));
    expect(screen.getByText("Voice editor")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Draft saved")).not.toBeInTheDocument();
  });

  it("preserves edits when leaving is cancelled and discards them only after confirmation", async () => {
    const user = mount();
    await user.type(await screen.findByRole("textbox", { name: "Natural conversation edit" }), "unsaved setting");
    await user.click(screen.getByRole("tab", { name: "Voice" }));
    expect(screen.getByRole("dialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
    expect(screen.queryByText("Voice editor")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("textbox", { name: "Natural conversation edit" })).toHaveValue("unsaved setting");
    expect(screen.getByRole("tab", { name: "Natural Conversation" })).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getByRole("tab", { name: "Voice" }));
    await user.click(screen.getByRole("button", { name: "Discard and leave" }));
    expect(screen.getByText("Voice editor")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Natural Conversation" }));
    expect(screen.getByRole("textbox", { name: "Natural conversation edit" })).toHaveValue("");
    await user.click(screen.getByRole("tab", { name: "Voice" }));
    expect(screen.getByText("Voice editor")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it.each([
    ["Test", "Testing panel"],
    ["Publish", "Publish panel"],
    ["Test saved conversation", "Testing panel"],
  ])("guards the %s action while the Natural Conversation editor is dirty", async (action, destination) => {
    const user = mount();
    await user.type(await screen.findByRole("textbox", { name: "Natural conversation edit" }), "unsaved setting");
    await user.click(screen.getByRole("button", { name: action }));
    expect(screen.getByRole("dialog", { name: "Discard unsaved changes?" })).toBeInTheDocument();
    expect(screen.queryByText(destination)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard and leave" }));
    expect(screen.getByText(destination)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Natural conversation edit" })).not.toBeInTheDocument();
  });

  it("keeps the editor open during a save and permits navigation after the save finishes", async () => {
    const user = mount();
    await user.type(await screen.findByRole("textbox", { name: "Natural conversation edit" }), "saving setting");
    await user.click(screen.getByRole("button", { name: "Start saving" }));
    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(toast).toHaveBeenCalledWith("Wait for natural conversation settings to finish saving.", "info");
    expect(screen.getByRole("textbox", { name: "Natural conversation edit" })).toHaveValue("saving setting");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Testing panel")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Finish saving" }));
    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(screen.getByText("Testing panel")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
