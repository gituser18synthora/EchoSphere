import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  HumanSpeechSettingsEditor,
  validateHumanSpeechOverrides,
} from "./HumanSpeechSettings";
import type {
  AmbiencePresetOption,
  HumanSpeechEffectiveSettings,
  HumanSpeechSettings,
  HumanSpeechSources,
} from "@/types/domain";

/** The backend registry's catalog (GET voice-settings `ambiencePresets`). */
const CATALOG: AmbiencePresetOption[] = [
  { id: "office", label: "Office", description: "Steady office air, a few distant colleagues talking, occasional typing.", productionEnabled: true },
  { id: "call_center", label: "Call Center", description: "Many agents talking almost continuously in a treated room; little typing.", productionEnabled: true },
  { id: "light_office", label: "Light Office", description: "A calm, sparsely occupied office: soft air, the odd far-away voice, a little typing.", productionEnabled: true },
  { id: "busy_office", label: "Busy Office", description: "A lively open-plan floor: more and nearer voices, frequent typing, footsteps, chairs, paper.", productionEnabled: true },
  { id: "room_tone", label: "Room Tone", description: "Neutral room air and ventilation only: no voices, no events.", productionEnabled: true },
  { id: "echo_ringing", label: "Echo Ringing", description: "A reverberant room: a warbling ring tone over a steady murmur.", productionEnabled: true },
];
/** The same catalog after a preset was withdrawn (registry flag off). */
const WITHDRAWN: AmbiencePresetOption[] = CATALOG.map((preset) => (
  preset.id === "echo_ringing" ? { ...preset, productionEnabled: false } : preset
));

const inherited: HumanSpeechEffectiveSettings = {
  enabled: true,
  thinking_fillers: true,
  acknowledgements: true,
  backchannels: true,
  prosody_variation: true,
  gender_agreement: true,
  micro_pauses: true,
  self_correction: false,
  breathing: true,
  filler_words: true,
  latency_fillers: true,
  sentence_breaths: true,
  breath_gain_db: 0,
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
  latency_filler_ladder: true,
  adaptive_latency_cues: false,
  latency_cue_probability: 0.7,
  latency_filler_hmm_ms: 3500,
  latency_filler_spoken_ms: 5000,
  background_ambience: false,
  background_ambience_preset: "office",
  background_ambience_volume: 50,
};

const platformSources = Object.fromEntries(
  Object.keys(inherited).map((key) => [key, "platform"]),
) as HumanSpeechSources;

describe("HumanSpeechSettingsEditor", () => {
  it("collapses advanced tuning while keeping common switches visible", async () => {
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ backchannel_probability: 0.25, latency_filler_delay_ms: 2000 }}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={() => undefined}
        collapseAdvanced
      />,
    );

    const summary = screen.getByText("Advanced tuning").closest("summary");
    const details = summary?.closest("details");
    expect(summary).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(within(summary as HTMLElement).getByText("· 2 overrides")).toBeVisible();
    expect(screen.getByRole("switch", { name: "Thinking fillers" })).toBeVisible();
    const probability = screen.getByLabelText("Backchannel probability");
    expect(probability).not.toBeVisible();
    expect(probability).toHaveValue(0.25);

    await userEvent.click(summary as HTMLElement);

    expect(details).toHaveAttribute("open");
    expect(probability).toBeVisible();
    expect(screen.getByLabelText("Delay before the first gap sound (ms)")).toHaveValue(2000);
  });

  it("preserves hidden numeric overrides when a common switch changes", async () => {
    const onChange = vi.fn();
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ backchannel_probability: 0.25, latency_filler_delay_ms: 2000 }}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={onChange}
        collapseAdvanced
      />,
    );

    expect(screen.getByLabelText("Backchannel probability")).not.toBeVisible();
    await userEvent.click(screen.getByRole("switch", { name: "Thinking fillers" }));
    expect(onChange).toHaveBeenLastCalledWith({
      backchannel_probability: 0.25,
      latency_filler_delay_ms: 2000,
      thinking_fillers: false,
    });
  });

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
    expect(screen.getByText("Thinking fillers")).toBeVisible();
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
        collapseAdvanced
      />,
    );

    const toggle = screen.getByRole("switch", { name: "Thinking fillers" });
    expect(screen.getByLabelText("Backchannel probability")).not.toBeVisible();
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
    expect(probability).toBeVisible();
    expect(screen.queryByText("Advanced tuning")).not.toBeInTheDocument();
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

  it("exposes the pre-reply breath switch and its delay with backend bounds", async () => {
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

    await userEvent.click(screen.getByRole("switch", { name: "Breath before the reply" }));
    expect(onChange).toHaveBeenLastCalledWith({ latency_fillers: false });
    const delay = screen.getByRole("spinbutton", { name: "Delay before the first gap sound (ms)" });
    expect(delay).toHaveValue(1500);
    expect(delay).toHaveAttribute("min", "500");
    expect(delay).toHaveAttribute("max", "5000");
    expect(validateHumanSpeechOverrides({ latency_filler_delay_ms: 300 })).toEqual([
      "Delay before the first gap sound (ms) must be between 500 and 5000.",
    ]);
    expect(validateHumanSpeechOverrides({ latency_filler_delay_ms: 1500.5 })).toEqual([
      "Delay before the first gap sound (ms) must be between 500 and 5000.",
    ]);
    expect(validateHumanSpeechOverrides({ latency_filler_delay_ms: 2000 })).toEqual([]);
    const cueProbability = screen.getByRole("spinbutton", { name: "Long-wait cue probability" });
    expect(cueProbability).toHaveValue(0.7);
    expect(validateHumanSpeechOverrides({ latency_cue_probability: 1.2 })).toEqual([
      "Long-wait cue probability must be between 0 and 1.",
    ]);
  });

  it("separates Breathing and Filler words into independent sections with their own masters", async () => {
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

    const breathing = screen.getByTestId("human-speech-group-breathing");
    const words = screen.getByTestId("human-speech-group-filler-words");
    expect(within(breathing).getByRole("switch", { name: "Breathing" })).toBeVisible();
    expect(within(breathing).getByRole("switch", { name: "Breath before the reply" })).toBeVisible();
    expect(within(breathing).getByRole("switch", { name: "Breath inside long replies" })).toBeVisible();
    expect(within(breathing).getByRole("spinbutton", { name: "Breathing volume (dB)" })).toBeVisible();
    expect(within(words).getByRole("switch", { name: "Filler words" })).toBeVisible();
    expect(within(words).getByRole("switch", { name: "Acknowledgements" })).toBeVisible();
    expect(within(words).getByRole("switch", { name: "Thinking cues on long waits" })).toBeVisible();
    expect(within(words).getByRole("spinbutton", { name: "Thinking cue at (ms)" })).toBeVisible();
    expect(within(words).queryByRole("switch", { name: /Breath/ })).toBeNull();
    expect(within(breathing).queryByRole("switch", { name: /Acknowledgements|Thinking/ })).toBeNull();

    // Turning one family off writes only its own key.
    await userEvent.click(within(breathing).getByRole("switch", { name: "Breathing" }));
    expect(onChange).toHaveBeenLastCalledWith({ breathing: false });
    await userEvent.click(within(words).getByRole("switch", { name: "Filler words" }));
    expect(onChange).toHaveBeenLastCalledWith({ filler_words: false });
  });

  it("offers Background ambience under Call environment, off by default, with sound and volume disabled", async () => {
    const onChange = vi.fn();
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{}}
        inherited={inherited}
        inheritedSources={platformSources}
        ambiencePresets={CATALOG}
        onChange={onChange}
      />,
    );

    const environment = screen.getByTestId("human-speech-group-ambience");
    expect(within(environment).getByText("Call environment")).toBeVisible();
    const toggle = within(environment).getByRole("switch", { name: "Background ambience" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(within(environment).getByText("Effective: Off · source: platform")).toBeVisible();
    // Sound and volume sit beneath the switch, inactive while it is off.
    const sound = within(environment).getByRole("combobox", { name: "Background sound" });
    const volume = within(environment).getByRole("slider", { name: "Background volume" });
    expect(sound).toBeDisabled();
    expect(volume).toBeDisabled();
    expect(sound).toHaveValue("office");
    expect(volume).toHaveValue("50");
    expect(screen.getByTestId("human-speech-inactive-background_ambience_preset")).toHaveTextContent(
      "Inactive while Background ambience is off.",
    );
    expect(screen.getByTestId("human-speech-inactive-background_ambience_volume")).toBeInTheDocument();
    // No hidden advanced-tuning field for them either.
    expect(within(environment).queryByRole("spinbutton")).toBeNull();

    await userEvent.click(toggle);
    expect(onChange).toHaveBeenLastCalledWith({ background_ambience: true });
  });

  it("enables sound and volume as soon as ambience is on and saves them as sparse overrides", async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ background_ambience: true }}
        inherited={inherited}
        inheritedSources={platformSources}
        ambiencePresets={CATALOG}
        onChange={onChange}
      />,
    );

    const sound = screen.getByRole("combobox", { name: "Background sound" });
    const volume = screen.getByRole("slider", { name: "Background volume" });
    expect(sound).toBeEnabled();
    expect(volume).toBeEnabled();
    // Every offered preset from the registry catalog, in registry order.
    expect(within(sound).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Office", "Call Center", "Light Office", "Busy Office", "Room Tone", "Echo Ringing",
    ]);
    expect(screen.queryByTestId("human-speech-inactive-background_ambience_preset")).toBeNull();

    await userEvent.selectOptions(sound, "call_center");
    expect(onChange).toHaveBeenLastCalledWith({ background_ambience: true, background_ambience_preset: "call_center" });
    fireEvent.change(volume, { target: { value: "70" } });
    expect(onChange).toHaveBeenLastCalledWith({ background_ambience: true, background_ambience_volume: 70 });

    rerender(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ background_ambience: true, background_ambience_preset: "room_tone", background_ambience_volume: 0 }}
        inherited={inherited}
        inheritedSources={platformSources}
        ambiencePresets={CATALOG}
        onChange={onChange}
      />,
    );
    expect(screen.getByRole("combobox", { name: "Background sound" })).toHaveValue("room_tone");
    const controls = screen.getByTestId("human-speech-control-background_ambience_volume");
    expect(within(controls).getByText("Effective: Muted · source: bot")).toBeInTheDocument();
    // Inherit drops only the volume override.
    await userEvent.click(within(controls).getByRole("button", { name: "Inherit" }));
    expect(onChange).toHaveBeenLastCalledWith({ background_ambience: true, background_ambience_preset: "room_tone" });
  });

  it("shows inherited tenant sound and volume with their source", () => {
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{}}
        inherited={{ ...inherited, background_ambience: true, background_ambience_preset: "busy_office", background_ambience_volume: 30 }}
        inheritedSources={{ ...platformSources, background_ambience: "tenant", background_ambience_preset: "tenant", background_ambience_volume: "tenant" }}
        ambiencePresets={CATALOG}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("switch", { name: "Background ambience" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("combobox", { name: "Background sound" })).toHaveValue("busy_office");
    expect(screen.getByText("Effective: Busy Office · source: tenant")).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Background volume" })).toHaveValue("30");
    expect(screen.getByText("Effective: 30 · source: tenant")).toBeInTheDocument();
  });

  it("shows Background ambience inactive while the Human speech layer is off", () => {
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ enabled: false, background_ambience: true }}
        inherited={inherited}
        inheritedSources={platformSources}
        ambiencePresets={CATALOG}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("switch", { name: "Background ambience" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("human-speech-inactive-background_ambience")).toHaveTextContent(
      "Inactive while the Human speech layer is off.",
    );
    expect(screen.getByRole("combobox", { name: "Background sound" })).toBeDisabled();
    expect(screen.getByTestId("human-speech-inactive-background_ambience_volume")).toHaveTextContent(
      "Inactive while the Human speech layer is off.",
    );
  });

  it("treats ambience settings missing from an older backend snapshot as off / Office / 50", () => {
    const {
      background_ambience: _a, background_ambience_preset: _p, background_ambience_volume: _v, ...older
    } = inherited;
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{}}
        inherited={older as HumanSpeechEffectiveSettings}
        inheritedSources={platformSources}
        ambiencePresets={CATALOG}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("switch", { name: "Background ambience" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("combobox", { name: "Background sound" })).toHaveValue("office");
    expect(screen.getByRole("slider", { name: "Background volume" })).toHaveValue("50");
  });

  it("selects and saves Echo Ringing like any other sound", async () => {
    const onChange = vi.fn();
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ background_ambience: true }}
        inherited={inherited}
        inheritedSources={platformSources}
        ambiencePresets={CATALOG}
        onChange={onChange}
      />,
    );
    const sound = screen.getByRole("combobox", { name: "Background sound" });
    expect(within(sound).getByRole("option", { name: "Echo Ringing" })).toBeEnabled();
    await userEvent.selectOptions(sound, "echo_ringing");
    expect(onChange).toHaveBeenLastCalledWith({ background_ambience: true, background_ambience_preset: "echo_ringing" });
    expect(validateHumanSpeechOverrides({ background_ambience_preset: "echo_ringing" }, { ambiencePresets: CATALOG })).toEqual([]);
  });

  it("keeps a saved preset that is no longer offered visible, but never offers it again", async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ background_ambience: true, background_ambience_preset: "echo_ringing" }}
        inherited={inherited}
        inheritedSources={platformSources}
        ambiencePresets={WITHDRAWN}
        onChange={onChange}
      />,
    );

    const sound = screen.getByRole("combobox", { name: "Background sound" });
    expect(sound).toHaveValue("echo_ringing");
    const saved = within(sound).getByRole("option", { name: "Echo Ringing (not available)" });
    expect(saved).toBeDisabled();
    expect(screen.getByText("Effective: Echo Ringing · source: bot")).toBeInTheDocument();
    expect(screen.getByTestId("human-speech-unavailable-background_ambience_preset")).toHaveTextContent(
      "Echo Ringing is no longer offered. It stays saved until you choose another sound.",
    );
    await userEvent.selectOptions(sound, "office");
    expect(onChange).toHaveBeenLastCalledWith({ background_ambience: true, background_ambience_preset: "office" });

    rerender(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ background_ambience: true, background_ambience_preset: "office" }}
        inherited={inherited}
        inheritedSources={platformSources}
        ambiencePresets={WITHDRAWN}
        onChange={onChange}
      />,
    );
    const options = within(screen.getByRole("combobox", { name: "Background sound" })).getAllByRole("option");
    expect(options.map((option) => option.textContent)).not.toContain("Echo Ringing");
    expect(options.map((option) => option.textContent)).not.toContain("Echo Ringing (not available)");
    expect(screen.queryByTestId("human-speech-unavailable-background_ambience_preset")).toBeNull();
  });

  it("rejects out-of-range volumes and unknown sounds before saving", () => {
    const catalogs = { ambiencePresets: CATALOG };
    expect(validateHumanSpeechOverrides({ background_ambience_volume: 0 })).toEqual([]);
    expect(validateHumanSpeechOverrides({ background_ambience_volume: 100 })).toEqual([]);
    expect(validateHumanSpeechOverrides({ background_ambience_preset: "call_center" }, catalogs)).toEqual([]);
    // A value saved before its preset was withdrawn is not a client-side error
    // (the server refuses it only as a NEW choice).
    expect(validateHumanSpeechOverrides({ background_ambience_preset: "echo_ringing" }, { ambiencePresets: WITHDRAWN })).toEqual([]);
    for (const volume of [-1, 101, 12.5, Number.NaN]) {
      expect(validateHumanSpeechOverrides({ background_ambience_volume: volume })).toEqual([
        "Background volume must be a whole number between 0 and 100.",
      ]);
    }
    expect(validateHumanSpeechOverrides({ background_ambience_volume: "70" as never })).toHaveLength(1);
    expect(validateHumanSpeechOverrides({ background_ambience_preset: "jungle" }, catalogs)).toEqual([
      "Background sound must be one of Office, Call Center, Light Office, Busy Office, Room Tone, Echo Ringing.",
    ]);
    // Without a catalog (older API) the server is the only judge of the id.
    expect(validateHumanSpeechOverrides({ background_ambience_preset: "jungle" })).toEqual([]);
  });

  it("marks a family's members inactive while its master is off, without touching the other family", () => {
    render(
      <HumanSpeechSettingsEditor
        scope="bot"
        override={{ breathing: false }}
        inherited={inherited}
        inheritedSources={platformSources}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole("switch", { name: "Breathing" })).toHaveAttribute("aria-checked", "false");
    // Children keep their own saved value…
    expect(screen.getByRole("switch", { name: "Breath before the reply" })).toHaveAttribute("aria-checked", "true");
    // …and say why they are silent right now.
    expect(screen.getByTestId("human-speech-inactive-latency_fillers")).toHaveTextContent("Inactive while Breathing is off.");
    expect(screen.getByTestId("human-speech-inactive-sentence_breaths")).toBeInTheDocument();
    // Filler words are unaffected.
    expect(screen.queryByTestId("human-speech-inactive-acknowledgements")).toBeNull();
    expect(screen.queryByTestId("human-speech-inactive-latency_filler_ladder")).toBeNull();
    expect(screen.queryByTestId("human-speech-inactive-filler_words")).toBeNull();
    expect(screen.getByRole("switch", { name: "Filler words" })).toHaveAttribute("aria-checked", "true");
  });

  it("allows quieter breathing and rejects amplification", () => {
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

    const volume = screen.getByRole("spinbutton", { name: "Breathing volume (dB)" });
    expect(volume).toHaveValue(0);
    expect(volume).toHaveAttribute("min", "-24");
    expect(volume).toHaveAttribute("max", "0");
    expect(volume).toHaveAttribute("step", "0.5");

    fireEvent.change(volume, { target: { value: "-6" } });
    expect(onChange).toHaveBeenLastCalledWith({ breath_gain_db: -6 });
    expect(validateHumanSpeechOverrides({ breath_gain_db: -6 })).toEqual([]);
    expect(validateHumanSpeechOverrides({ breath_gain_db: -6.5 })).toEqual([]);
    expect(validateHumanSpeechOverrides({ breath_gain_db: 1 })).toEqual([
      "Breathing volume (dB) must be between -24 and 0.",
    ]);
    expect(validateHumanSpeechOverrides({ breath_gain_db: Number.NaN })).toEqual([
      "Breathing volume (dB) must be between -24 and 0.",
    ]);
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
