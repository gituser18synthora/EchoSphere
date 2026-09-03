import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  HumanSpeechSettingsEditor,
  validateHumanSpeechOverrides,
} from "./HumanSpeechSettings";
import type {
  HumanSpeechEffectiveSettings,
  HumanSpeechSettings,
  HumanSpeechSources,
} from "@/types/domain";

const inherited: HumanSpeechEffectiveSettings = {
  enabled: true,
  thinking_fillers: true,
  acknowledgements: true,
  backchannels: true,
  prosody_variation: true,
  gender_agreement: true,
  micro_pauses: true,
  self_correction: false,
  latency_fillers: true,
  sentence_breaths: true,
  thinking_filler_probability: 0.25,
  acknowledgement_probability: 0.4,
  tool_ack_probability: 0.9,
  backchannel_probability: 0.35,
  micro_pause_probability: 0.45,
  self_correction_probability: 0.01,
  sentence_breath_probability: 0.2,
  min_long_turn_for_backchannel_ms: 4000,
  min_gap_between_backchannels_ms: 8000,
  max_backchannels_per_call: 4,
  latency_filler_delay_ms: 1500,
};

const platformSources = Object.fromEntries(
  Object.keys(inherited).map((key) => [key, "platform"]),
) as HumanSpeechSources;

describe("HumanSpeechSettingsEditor", () => {
  it("shows effective inherited values and creates a sparse override", async () => {
    const onChange = vi.fn();
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{}}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={onChange}
      />,
    );

    expect(
      screen.getAllByText("Effective: On · source: platform").length,
    ).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("switch", { name: "Thinking fillers" }));
    expect(onChange).toHaveBeenLastCalledWith({ thinking_fillers: false });
  });

  it("clears one bot value back to inheritance without losing other fields", async () => {
    const onChange = vi.fn();
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ thinking_fillers: false, backchannel_probability: 0.25 }}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={onChange}
      />,
    );

    const toggle = screen.getByRole("switch", { name: "Thinking fillers" });
    const card = toggle.closest(".card-pad-sm");
    expect(card).not.toBeNull();
    await userEvent.click(within(card as HTMLElement).getByRole("button", { name: "Inherit" }));
    expect(onChange).toHaveBeenLastCalledWith({ backchannel_probability: 0.25 });
  });

  it("uses the same numeric bounds as the backend", () => {
    render(
      <HumanSpeechSettingsEditor
        scope="tenant"
        override={{}}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={() => undefined}
      />,
    );

    const probability = screen.getByRole("spinbutton", { name: "Backchannel probability" });
    expect(probability).toHaveAttribute("min", "0");
    expect(probability).toHaveAttribute("max", "1");
    const gap = screen.getByRole("spinbutton", { name: "Minimum backchannel gap (ms)" });
    expect(gap).toHaveAttribute("min", "2000");
    expect(gap).toHaveAttribute("max", "120000");
    const maximum = screen.getByRole("spinbutton", { name: "Maximum backchannels per call" });
    expect(maximum).toHaveAttribute("min", "0");
    expect(maximum).toHaveAttribute("max", "20");
    expect(validateHumanSpeechOverrides({ backchannel_probability: 1.01 })).toEqual([
      "Backchannel probability must be between 0 and 1.",
    ]);
    expect(validateHumanSpeechOverrides({ max_backchannels_per_call: 2.5 })).toEqual([
      "Maximum backchannels per call must be between 0 and 20.",
    ]);
  });

  it("exposes the latency filler switch and its delay with backend bounds", async () => {
    const onChange = vi.fn();
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{}}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={onChange}
      />,
    );

    await userEvent.click(screen.getByRole("switch", { name: "Latency fillers" }));
    expect(onChange).toHaveBeenLastCalledWith({ latency_fillers: false });
    const delay = screen.getByRole("spinbutton", { name: "Latency filler delay (ms)" });
    expect(delay).toHaveValue(1500);
    expect(delay).toHaveAttribute("min", "500");
    expect(delay).toHaveAttribute("max", "5000");
    expect(validateHumanSpeechOverrides({ latency_filler_delay_ms: 300 })).toEqual([
      "Latency filler delay (ms) must be between 500 and 5000.",
    ]);
    expect(validateHumanSpeechOverrides({ latency_filler_delay_ms: 1500.5 })).toEqual([
      "Latency filler delay (ms) must be between 500 and 5000.",
    ]);
    expect(validateHumanSpeechOverrides({ latency_filler_delay_ms: 2000 })).toEqual([]);
  });

  it("preserves fields a future form version may not understand", async () => {
    const onChange = vi.fn();
    const override = {
      enabled: true,
      future_delivery_control: 0.7,
    } as HumanSpeechSettings;
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={override}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={onChange}
      />,
    );

    await userEvent.click(screen.getByRole("switch", { name: "Thinking fillers" }));
    expect(onChange).toHaveBeenLastCalledWith({
      enabled: true,
      future_delivery_control: 0.7,
      thinking_fillers: false,
    });
  });

  it("clears all sparse overrides", async () => {
    const onChange = vi.fn();
    render(
      <HumanSpeechSettingsEditor
        scope="tenant"
        override={{ enabled: false, tool_ack_probability: 0.5 }}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={onChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Clear all overrides" }));
    expect(onChange).toHaveBeenLastCalledWith({});
  });
});
