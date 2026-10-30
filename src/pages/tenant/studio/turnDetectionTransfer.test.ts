import { describe, expect, it } from "vitest";
import {
  TURN_DETECTION_EXPORT_KIND,
  buildTurnDetectionExport,
  parseTurnDetectionImport,
} from "./turnDetectionTransfer";
import type { TurnDetectionConfig } from "@/types/domain";

const CONFIG: TurnDetectionConfig = {
  schemaVersion: 1,
  mode: "system_default",
  overrides: {},
  effective: {
    browser: { turn_detection: { confidence: 0.7 }, noise_gate: { enabled: 1 } },
    telephony: { turn_detection: { confidence: 0.6 }, noise_gate: { enabled: 1 } },
  },
  transports: [
    { id: "browser", label: "Browser", description: "Browser microphones." },
    { id: "telephony", label: "Telephony", description: "PSTN and SIP calls." },
  ],
  modes: [
    { id: "system_default", label: "System Default", description: "Runtime defaults." },
    { id: "recommended", label: "Recommended", description: "Balanced profile." },
    { id: "custom", label: "Custom", description: "Tenant overrides." },
  ],
  sections: [{ id: "speech_detection", label: "Speech Detection", description: "Detect speech." }],
  fields: [
    {
      group: "turn_detection", key: "confidence", section: "speech_detection",
      label: "VAD confidence", description: "Voice confidence.", input: "slider",
      valueType: "number", unit: "ratio", min: 0.3, max: 0.95, step: 0.01,
      default: { browser: 0.7, telephony: 0.6 },
      recommended: { browser: 0.65, telephony: 0.58 },
    },
    {
      group: "turn_detection", key: "barge_in_min_words", section: "speech_detection",
      label: "Barge-in word threshold", description: "Words before barge-in.", input: "number",
      valueType: "integer", unit: "words", min: 0, max: 10, step: 1,
      default: { browser: 2, telephony: 2 },
      recommended: { browser: 2, telephony: 2 },
    },
    {
      group: "noise_gate", key: "enabled", section: "speech_detection",
      label: "Adaptive noise gate", description: "Enable gate.", input: "toggle",
      valueType: "boolean", unit: "on/off", min: 0, max: 1, step: 1,
      default: { browser: 1, telephony: 1 },
      recommended: { browser: 1, telephony: 1 },
    },
  ],
};

describe("buildTurnDetectionExport", () => {
  it("exports only portable configuration — kind, schemaVersion, mode, overrides", () => {
    const text = buildTurnDetectionExport(CONFIG, "custom", {
      browser: { turn_detection: { confidence: 0.8 } },
    });
    const doc = JSON.parse(text);
    expect(Object.keys(doc).sort()).toEqual(["kind", "mode", "overrides", "schemaVersion"]);
    expect(doc).toEqual({
      kind: TURN_DETECTION_EXPORT_KIND,
      schemaVersion: 1,
      mode: "custom",
      overrides: { browser: { turn_detection: { confidence: 0.8 } } },
    });
  });

  it("non-custom modes export no values so the target resolves its own profile", () => {
    const doc = JSON.parse(buildTurnDetectionExport(CONFIG, "recommended", {
      browser: { turn_detection: { confidence: 0.8 } },
    }));
    expect(doc.mode).toBe("recommended");
    expect(doc.overrides).toEqual({});
  });

  it("round-trips through the import parser", () => {
    const text = buildTurnDetectionExport(CONFIG, "custom", {
      telephony: { turn_detection: { confidence: 0.75, barge_in_min_words: 3 }, noise_gate: { enabled: false } },
    });
    const result = parseTurnDetectionImport(CONFIG, text);
    expect(result.errors).toEqual([]);
    expect(result.document).toEqual({
      mode: "custom",
      overrides: {
        telephony: { turn_detection: { confidence: 0.75, barge_in_min_words: 3 }, noise_gate: { enabled: false } },
      },
    });
  });
});

describe("parseTurnDetectionImport", () => {
  const parse = (value: unknown) => parseTurnDetectionImport(CONFIG, JSON.stringify(value));

  it("rejects text that is not JSON or not an object", () => {
    expect(parseTurnDetectionImport(CONFIG, "{ nope").errors[0]).toMatch(/Not valid JSON/);
    expect(parse([1, 2]).errors[0]).toMatch(/must be a JSON object/);
  });

  it("rejects a document with a foreign kind", () => {
    const result = parse({ kind: "echosphere.voice-settings", mode: "custom" });
    expect(result.document).toBeNull();
    expect(result.errors[0]).toMatch(/not a Turn Detection export/);
  });

  it("rejects a newer schema version and unknown top-level properties", () => {
    const result = parse({ kind: TURN_DETECTION_EXPORT_KIND, schemaVersion: 99, mode: "custom", tenantId: "tn_x" });
    expect(result.errors).toEqual(expect.arrayContaining([
      expect.stringMatching(/schema version 99/),
      expect.stringMatching(/Unknown property 'tenantId'/),
    ]));
  });

  it("rejects an unknown mode", () => {
    expect(parse({ mode: "turbo" }).errors[0]).toMatch(/mode must be one of/);
  });

  it("rejects unknown transports, groups and parameters", () => {
    const result = parse({
      mode: "custom",
      overrides: {
        cellular: {},
        browser: { magic: {}, turn_detection: { bogus: 1 } },
      },
    });
    expect(result.errors).toEqual(expect.arrayContaining([
      "Unknown transport 'cellular'.",
      "Browser: unknown settings group 'magic'.",
      "Browser: unknown parameter 'turn_detection.bogus'.",
    ]));
    expect(result.document).toBeNull();
  });

  it("rejects out-of-range, non-numeric, fractional-integer and non-boolean values", () => {
    const result = parse({
      mode: "custom",
      overrides: {
        browser: { turn_detection: { confidence: 5, barge_in_min_words: 2.5 } },
        telephony: { turn_detection: { confidence: "high" }, noise_gate: { enabled: "yes" } },
      },
    });
    expect(result.errors).toEqual(expect.arrayContaining([
      "Browser · VAD confidence: must be between 0.3 and 0.95 ratio.",
      "Browser · Barge-in word threshold: must be a whole number.",
      "Telephony · VAD confidence: must be a number.",
      "Telephony · Adaptive noise gate: must be true or false.",
    ]));
    expect(result.document).toBeNull();
  });

  it("accepts a bare mode/overrides document and normalizes value types", () => {
    const result = parse({
      mode: "custom",
      overrides: { browser: { noise_gate: { enabled: 0 }, turn_detection: { barge_in_min_words: 4 } } },
    });
    expect(result.errors).toEqual([]);
    expect(result.document).toEqual({
      mode: "custom",
      overrides: { browser: { noise_gate: { enabled: false }, turn_detection: { barge_in_min_words: 4 } } },
    });
  });

  it("warns when a non-custom mode carries overrides, and drops them", () => {
    const result = parse({
      mode: "recommended",
      overrides: { browser: { turn_detection: { confidence: 0.8 } } },
    });
    expect(result.errors).toEqual([]);
    expect(result.warnings[0]).toMatch(/overrides in this document are ignored/);
    expect(result.document).toEqual({ mode: "recommended", overrides: {} });
  });

  it("still validates override values even in non-custom modes", () => {
    const result = parse({
      mode: "recommended",
      overrides: { browser: { turn_detection: { confidence: 5 } } },
    });
    expect(result.errors[0]).toMatch(/must be between/);
    expect(result.document).toBeNull();
  });
});
