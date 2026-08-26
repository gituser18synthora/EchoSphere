import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TurnDetectionTab from "./TurnDetectionTab";
import * as api from "@/services/api";
import type { TurnDetectionConfig } from "@/types/domain";

vi.mock("@/services/api", () => ({
  getTurnDetectionSettings: vi.fn(),
  saveTurnDetectionSettings: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn() }),
}));

const CONFIG: TurnDetectionConfig = {
  schemaVersion: 1,
  mode: "custom",
  overrides: {
    browser: {
      turn_detection: { confidence: 0.8 },
      noise_gate: { noise_margin_db: 12 },
    },
  },
  effective: {
    browser: {
      turn_detection: { confidence: 0.8 },
      noise_gate: { noise_margin_db: 12, enabled: 1 },
    },
    telephony: {
      turn_detection: { confidence: 0.6 },
      noise_gate: { noise_margin_db: 8, enabled: 1 },
    },
  },
  transports: [
    { id: "browser", label: "Browser", description: "Browser microphones." },
    { id: "telephony", label: "Telephony", description: "PSTN and SIP calls." },
  ],
  modes: [
    { id: "system_default", label: "System Default", description: "Runtime defaults." },
    { id: "recommended", label: "Recommended", description: "Balanced production profile." },
    { id: "custom", label: "Custom", description: "Sparse tenant overrides." },
  ],
  sections: [
    { id: "speech_detection", label: "Speech Detection", description: "Detect speech." },
    { id: "noise_suppression", label: "Noise Suppression", description: "Reject noise." },
  ],
  fields: [
    {
      group: "turn_detection", key: "confidence", section: "speech_detection",
      label: "VAD confidence", description: "Voice confidence.", input: "slider",
      valueType: "number", unit: "ratio", min: 0.3, max: 0.95, step: 0.01,
      default: { browser: 0.7, telephony: 0.6 },
      recommended: { browser: 0.65, telephony: 0.58 },
    },
    {
      group: "noise_gate", key: "noise_margin_db", section: "noise_suppression",
      label: "Noise-floor margin", description: "Margin over noise.", input: "number",
      valueType: "number", unit: "dB", min: 3, max: 24, step: 0.5,
      default: { browser: 10, telephony: 8 },
      recommended: { browser: 9, telephony: 8 },
    },
    {
      group: "noise_gate", key: "enabled", section: "noise_suppression",
      label: "Adaptive noise gate", description: "Enable gate.", input: "toggle",
      valueType: "boolean", unit: "on/off", min: 0, max: 1, step: 1,
      default: { browser: 1, telephony: 1 },
      recommended: { browser: 1, telephony: 1 },
    },
  ],
};

describe("TurnDetectionTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getTurnDetectionSettings).mockResolvedValue(CONFIG);
    vi.mocked(api.saveTurnDetectionSettings).mockResolvedValue(CONFIG);
  });

  it("shows effective values, range, default and recommended per transport", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    expect(await screen.findByLabelText("VAD confidence value")).toHaveValue(0.8);
    expect(screen.getByText("Range 0.3–0.95 ratio")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Default 0.7" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recommended 0.65" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Telephony" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.6);
    expect(screen.getByRole("button", { name: "Default 0.6" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recommended 0.58" })).toBeInTheDocument();
    expect(screen.getByText("PSTN and SIP calls.")).toBeInTheDocument();
  });

  it("Default and Recommended quick-set buttons update the draft, and Default drops the override", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Recommended 0.65" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.65);
    await user.click(screen.getByRole("button", { name: "Default 0.7" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.7);
    expect(screen.getByRole("button", { name: "Default 0.7" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Save Changes" }));
    // The confidence override is removed rather than stored as an explicit
    // default-equal value, so the saved document stays sparse.
    await waitFor(() => expect(api.saveTurnDetectionSettings).toHaveBeenCalledWith(
      "custom",
      { browser: { noise_gate: { noise_margin_db: 12 } } },
    ));
  });

  it("saves the data-driven Recommended profile marker without copied values", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Use Recommended Settings" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.65);
    await user.click(screen.getByRole("button", { name: "Save Changes" }));
    await waitFor(() => expect(api.saveTurnDetectionSettings).toHaveBeenCalledWith("recommended", {}));
  });

  it("Reset Section removes only fields in that section", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    const speech = (await screen.findByText("Speech Detection")).closest("section")!;
    await user.click(within(speech).getByRole("button", { name: "Reset Section" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.7);
    expect(screen.getByLabelText("Noise-floor margin value")).toHaveValue(12);
    await user.click(screen.getByRole("button", { name: "Save Changes" }));
    await waitFor(() => expect(api.saveTurnDetectionSettings).toHaveBeenCalledWith(
      "custom",
      { browser: { noise_gate: { noise_margin_db: 12 } } },
    ));
  });

  it("Reset All asks for confirmation, then stores System Default with no overrides", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Reset All to Default" }));
    expect(screen.getByText("Reset all turn detection settings?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset All" }));
    await user.click(screen.getByRole("button", { name: "Save Changes" }));
    await waitFor(() => expect(api.saveTurnDetectionSettings).toHaveBeenCalledWith("system_default", {}));
  });

  it("cancelling Reset All keeps the current draft untouched", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Reset All to Default" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.8);
    expect(screen.getByRole("button", { name: "Save Changes" })).toBeDisabled();
  });

  it("never allows a custom value outside the server-provided bounds", async () => {
    render(<TurnDetectionTab />);
    const input = await screen.findByLabelText("VAD confidence value");
    fireEvent.change(input, { target: { value: "2" } });
    // The draft (and the synced slider) clamp immediately; the free-typed text
    // is flagged and replaced by the clamped value once the field is left.
    expect(screen.getByLabelText("VAD confidence slider")).toHaveValue("0.95");
    fireEvent.blur(input);
    expect(input).toHaveValue(0.95);
  });

  it("save stays disabled until something changes, and Discard restores the saved draft", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    const input = await screen.findByLabelText("VAD confidence value");
    expect(screen.getByRole("button", { name: "Save Changes" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "0.85" } });
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Changes" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.8);
    expect(screen.getByRole("button", { name: "Save Changes" })).toBeDisabled();
  });

  it("switching modes within a session preserves unsaved custom overrides", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Use Recommended Settings" }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.65);
    await user.click(screen.getByRole("radio", { name: /Custom/ }));
    expect(screen.getByLabelText("VAD confidence value")).toHaveValue(0.8);
  });

  it("warns about extreme values using only schema bounds and recommendations", async () => {
    render(<TurnDetectionTab />);
    const input = await screen.findByLabelText("VAD confidence value");
    fireEvent.change(input, { target: { value: "0.3" } });
    fireEvent.blur(input);
    expect(screen.getByText(/Extreme low value/)).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "0.7" } });
    fireEvent.blur(input);
    expect(screen.queryByText(/Extreme low value/)).not.toBeInTheDocument();
  });

  it("Copy Configuration puts a portable document on the clipboard — no tenant or bot ids", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Copy Configuration" }));
    const copied = JSON.parse(await navigator.clipboard.readText());
    expect(copied).toEqual({
      kind: "echosphere.turn-detection",
      schemaVersion: 1,
      mode: "custom",
      overrides: {
        browser: {
          turn_detection: { confidence: 0.8 },
          noise_gate: { noise_margin_db: 12 },
        },
      },
    });
  });

  it("import rejects invalid JSON, stays on the paste step and offers no Apply", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Import Configuration" }));
    fireEvent.change(screen.getByLabelText("Configuration JSON"), { target: { value: "{ not json" } });
    await user.click(screen.getByRole("button", { name: "Validate & Preview" }));
    expect(screen.getByText(/Not valid JSON/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply Configuration" })).not.toBeInTheDocument();
    expect(api.saveTurnDetectionSettings).not.toHaveBeenCalled();
  });

  it("import lists schema violations and never applies an invalid configuration", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Import Configuration" }));
    const pasted = JSON.stringify({
      kind: "echosphere.turn-detection",
      schemaVersion: 1,
      mode: "custom",
      overrides: { browser: { turn_detection: { confidence: 5, bogus: 1 } } },
    });
    fireEvent.change(screen.getByLabelText("Configuration JSON"), { target: { value: pasted } });
    await user.click(screen.getByRole("button", { name: "Validate & Preview" }));
    expect(screen.getByText("Browser · VAD confidence: must be between 0.3 and 0.95 ratio.")).toBeInTheDocument();
    expect(screen.getByText("Browser: unknown parameter 'turn_detection.bogus'.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply Configuration" })).not.toBeInTheDocument();
    expect(api.saveTurnDetectionSettings).not.toHaveBeenCalled();
  });

  it("valid import shows a diff-table preview and applies via the save API", async () => {
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Import Configuration" }));
    const pasted = JSON.stringify({
      kind: "echosphere.turn-detection",
      schemaVersion: 1,
      mode: "custom",
      overrides: { telephony: { turn_detection: { confidence: 0.75 } } },
    });
    fireEvent.change(screen.getByLabelText("Configuration JSON"), { target: { value: pasted } });
    await user.click(screen.getByRole("button", { name: "Validate & Preview" }));
    expect(screen.getByText("Valid configuration")).toBeInTheDocument();
    // Browser loses its two saved overrides; telephony confidence becomes 0.75.
    const dialog = screen.getByRole("dialog", { name: "Import Configuration" });
    expect(within(dialog).getByText("3 values will change")).toBeInTheDocument();
    expect(within(dialog).getByText("2 changes")).toBeInTheDocument();
    expect(within(dialog).getByText("1 change")).toBeInTheDocument();
    const rows = within(dialog).getAllByText("VAD confidence").map((el) => el.closest("tr")!);
    expect(rows[0]).toHaveTextContent("0.8 ratio");
    expect(rows[0]).toHaveTextContent("0.7 ratio");
    expect(rows[1]).toHaveTextContent("0.6 ratio");
    expect(rows[1]).toHaveTextContent("0.75 ratio");
    await user.click(screen.getByRole("button", { name: "Apply Configuration" }));
    await waitFor(() => expect(api.saveTurnDetectionSettings).toHaveBeenCalledWith(
      "custom",
      { telephony: { turn_detection: { confidence: 0.75 } } },
    ));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Import Configuration" })).not.toBeInTheDocument());
  });

  it("Paste from Clipboard validates the copied document straight to preview, and Edit JSON returns", async () => {
    const user = userEvent.setup();
    await navigator.clipboard.writeText(JSON.stringify({
      kind: "echosphere.turn-detection",
      schemaVersion: 1,
      mode: "recommended",
      overrides: {},
    }));
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Import Configuration" }));
    await user.click(screen.getByRole("button", { name: "Paste from Clipboard" }));
    expect(await screen.findByText("Valid configuration")).toBeInTheDocument();
    expect(screen.getByText("Recommended mode")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit JSON" }));
    const textarea = screen.getByLabelText("Configuration JSON") as HTMLTextAreaElement;
    expect(textarea.value).toContain('"mode":"recommended"');
    expect(screen.getByRole("button", { name: "Validate & Preview" })).toBeEnabled();
  });

  it("a failed apply keeps the modal open and reports that settings are unchanged", async () => {
    vi.mocked(api.saveTurnDetectionSettings).mockRejectedValue(new Error("Turn detection configuration is invalid."));
    const user = userEvent.setup();
    render(<TurnDetectionTab />);
    await screen.findByText("Speech Detection");
    await user.click(screen.getByRole("button", { name: "Import Configuration" }));
    fireEvent.change(screen.getByLabelText("Configuration JSON"), {
      target: { value: JSON.stringify({ mode: "recommended" }) },
    });
    await user.click(screen.getByRole("button", { name: "Validate & Preview" }));
    await user.click(screen.getByRole("button", { name: "Apply Configuration" }));
    expect(await screen.findByText("Could not apply configuration")).toBeInTheDocument();
    expect(screen.getByText(/Existing settings were left unchanged/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Import Configuration" })).toBeInTheDocument();
  });
});
