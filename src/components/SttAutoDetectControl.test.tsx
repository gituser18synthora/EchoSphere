/* Auto-detect language control + the platform-owned key rules in ProviderParams:
   - derived default (multilingual → on, single → off) when nothing is stored
   - an explicit stored value wins and shows "Set manually"
   - toggling writes an explicit boolean; "Use automatic" removes the key
   - schemaDefaults/reconcileSettings never pre-fill the key (legacy schema
     with `default: false` included) and carry an explicit choice across a
     model switch; ParamFields never renders it generically. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  AUTO_DETECT_LANGUAGE_KEY,
  SttAutoDetectControl,
  autoDetectState,
  derivedAutoDetectDefault,
} from "@/components/SttAutoDetectControl";
import { ParamFields, reconcileSettings, schemaDefaults } from "@/components/ProviderParams";
import type { ParamSpec } from "@/types/domain";

const LEGACY_SCHEMA: Record<string, ParamSpec> = {
  mode: { type: "enum", values: ["transcribe", "translate"], default: "transcribe", label: "Mode" },
  // Catalog rows seeded before the widget flag existed still carry a default.
  auto_detect_language: { type: "boolean", default: false, label: "Auto-detect language", advanced: true },
};
const NEW_SCHEMA: Record<string, ParamSpec> = {
  mode: { type: "enum", values: ["transcribe", "translate"], default: "transcribe", label: "Mode" },
  auto_detect_language: { type: "boolean", widget: "auto_detect_language", label: "Auto-detect language" },
};

describe("auto-detect state", () => {
  it("derives on for more than one language and off for one", () => {
    expect(derivedAutoDetectDefault(["en-IN", "hi-IN"])).toBe(true);
    expect(derivedAutoDetectDefault(["hi-IN"])).toBe(false);
    expect(derivedAutoDetectDefault([])).toBe(false);
    expect(autoDetectState({}, ["en-IN", "hi-IN"])).toEqual({
      effective: true, source: "derived", derivedDefault: true, explicit: null,
    });
  });

  it("an explicit stored value wins over the derived default", () => {
    expect(autoDetectState({ auto_detect_language: false }, ["en-IN", "hi-IN"])).toMatchObject({
      effective: false, source: "explicit", explicit: false, derivedDefault: true,
    });
    expect(autoDetectState({ auto_detect_language: true }, ["hi-IN"])).toMatchObject({
      effective: true, source: "explicit", derivedDefault: false,
    });
  });

  it("prefers the server-computed default (tenant inheritance) when given", () => {
    expect(autoDetectState({}, [], true).effective).toBe(true);
  });
});

describe("ProviderParams platform-owned key", () => {
  it("never pre-fills auto_detect_language, even from a legacy schema default", () => {
    expect(schemaDefaults(LEGACY_SCHEMA)).toEqual({ mode: "transcribe" });
    expect(schemaDefaults(NEW_SCHEMA)).toEqual({ mode: "transcribe" });
  });

  it("carries an explicit choice across a model switch and drops nothing else", () => {
    expect(reconcileSettings(LEGACY_SCHEMA, { auto_detect_language: false, mode: "translate" }))
      .toEqual({ mode: "translate", auto_detect_language: false });
    expect(reconcileSettings(NEW_SCHEMA, { mode: "translate" }))
      .toEqual({ mode: "translate" });
  });

  it("does not render the key as a generic field", () => {
    render(<ParamFields schema={LEGACY_SCHEMA} values={{}} onChange={() => undefined} />);
    expect(screen.queryByRole("switch", { name: "Auto-detect language" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Mode")).toBeInTheDocument();
  });
});

describe("<SttAutoDetectControl>", () => {
  it("shows the derived multilingual default as ON and explains why", () => {
    render(<SttAutoDetectControl settings={{}} languages={["en-IN", "hi-IN"]} onChange={() => undefined} />);
    expect(screen.getByRole("switch", { name: "Auto-detect language" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("stt-auto-detect-source")).toHaveTextContent("Automatic default: on (2 languages configured)");
    expect(screen.queryByRole("button", { name: /Use automatic/ })).not.toBeInTheDocument();
  });

  it("shows a single-language bot as OFF by default", () => {
    render(<SttAutoDetectControl settings={{}} languages={["hi-IN"]} onChange={() => undefined} />);
    expect(screen.getByRole("switch", { name: "Auto-detect language" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("stt-auto-detect-source")).toHaveTextContent("Automatic default: off (1 language configured)");
  });

  it("shows a persisted explicit value and lets the user return to automatic", async () => {
    const onChange = vi.fn();
    render(
      <SttAutoDetectControl
        settings={{ mode: "transcribe", [AUTO_DETECT_LANGUAGE_KEY]: false }}
        languages={["en-IN", "hi-IN"]} onChange={onChange}
      />,
    );
    expect(screen.getByRole("switch", { name: "Auto-detect language" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("stt-auto-detect-source")).toHaveTextContent("Set manually to off.");
    await userEvent.click(screen.getByRole("button", { name: /Use automatic/ }));
    expect(onChange).toHaveBeenCalledWith({ mode: "transcribe" });
  });

  it("toggling writes an explicit boolean into the settings", async () => {
    const onChange = vi.fn();
    render(<SttAutoDetectControl settings={{ mode: "transcribe" }} languages={["en-IN", "hi-IN"]} onChange={onChange} />);
    await userEvent.click(screen.getByRole("switch", { name: "Auto-detect language" }));
    expect(onChange).toHaveBeenCalledWith({ mode: "transcribe", auto_detect_language: false });
  });

  it("warns when an explicit STT language pins recognition anyway", () => {
    render(<SttAutoDetectControl settings={{}} languages={["en-IN", "hi-IN"]} sttLanguage="hi-IN" onChange={() => undefined} />);
    expect(screen.getByRole("note")).toHaveTextContent(/explicit STT language is selected/);
  });
});
