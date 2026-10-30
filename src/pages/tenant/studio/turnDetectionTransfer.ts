import type {
  TurnDetectionConfig,
  TurnDetectionGroup,
  TurnDetectionMode,
  TurnDetectionOverrides,
  TurnDetectionTransport,
} from "@/types/domain";

/** Marks a pasted JSON document as a Turn Detection export from any
    EchoSphere workspace, independent of tenant or environment. */
export const TURN_DETECTION_EXPORT_KIND = "echosphere.turn-detection";

/** The portable payload: configuration semantics only. Tenant ids, bot ids
    and database ids never appear here, so the same document can be applied
    to any bot in any workspace. */
export interface TurnDetectionDocument {
  mode: TurnDetectionMode;
  overrides: TurnDetectionOverrides;
}

export interface TurnDetectionImportResult {
  errors: string[];
  warnings: string[];
  document: TurnDetectionDocument | null;
}

const ENVELOPE_KEYS = new Set(["kind", "schemaVersion", "mode", "overrides"]);

const fmtNum = (value: number): string =>
  Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)));

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function buildTurnDetectionExport(
  config: TurnDetectionConfig,
  mode: TurnDetectionMode,
  overrides: TurnDetectionOverrides,
): string {
  return JSON.stringify(
    {
      kind: TURN_DETECTION_EXPORT_KIND,
      schemaVersion: config.schemaVersion,
      mode,
      // Non-custom modes carry no values by design: the target workspace
      // resolves its own defaults or recommended profile from the schema.
      overrides: mode === "custom" ? overrides : {},
    },
    null,
    2,
  );
}

/** Validate pasted JSON against the live schema served by the API and return
    a normalized document, or the full list of problems. Nothing is applied
    here — an invalid paste can never touch the stored configuration. */
export function parseTurnDetectionImport(
  config: TurnDetectionConfig,
  text: string,
): TurnDetectionImportResult {
  const fail = (errors: string[]): TurnDetectionImportResult => ({ errors, warnings: [], document: null });

  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return fail(["Not valid JSON — paste the configuration exactly as it was copied."]);
  }
  if (!isPlainObject(raw)) return fail(["The configuration must be a JSON object."]);
  if ("kind" in raw && raw.kind !== TURN_DETECTION_EXPORT_KIND) {
    return fail([`This JSON is not a Turn Detection export (expected kind "${TURN_DETECTION_EXPORT_KIND}").`]);
  }

  const errors: string[] = [];
  if ("schemaVersion" in raw) {
    if (typeof raw.schemaVersion !== "number") {
      errors.push("schemaVersion must be a number.");
    } else if (raw.schemaVersion > config.schemaVersion) {
      errors.push(
        `This export uses schema version ${raw.schemaVersion}; this workspace supports up to version ${config.schemaVersion}.`,
      );
    }
  }
  for (const key of Object.keys(raw)) {
    if (!ENVELOPE_KEYS.has(key)) {
      errors.push(`Unknown property '${key}' — expected only kind, schemaVersion, mode and overrides.`);
    }
  }

  const modeIds = new Set<string>(config.modes.map((mode) => mode.id));
  if (typeof raw.mode !== "string" || !modeIds.has(raw.mode)) {
    errors.push(`mode must be one of: ${config.modes.map((mode) => mode.id).join(", ")}.`);
  }

  const fieldsByPath = new Map(config.fields.map((field) => [`${field.group}.${field.key}`, field]));
  const knownGroups = new Set<string>(config.fields.map((field) => field.group));
  const transportLabels = new Map<string, string>(config.transports.map((item) => [item.id, item.label]));

  const normalized: TurnDetectionOverrides = {};
  const overridesRaw = raw.overrides ?? {};
  if (!isPlainObject(overridesRaw)) {
    errors.push("overrides must be an object.");
  } else {
    for (const [transport, groupsRaw] of Object.entries(overridesRaw)) {
      const transportLabel = transportLabels.get(transport);
      if (transportLabel === undefined) {
        errors.push(`Unknown transport '${transport}'.`);
        continue;
      }
      if (!isPlainObject(groupsRaw)) {
        errors.push(`${transportLabel}: overrides must be an object.`);
        continue;
      }
      for (const [group, valuesRaw] of Object.entries(groupsRaw)) {
        if (!knownGroups.has(group)) {
          errors.push(`${transportLabel}: unknown settings group '${group}'.`);
          continue;
        }
        if (!isPlainObject(valuesRaw)) {
          errors.push(`${transportLabel} · ${group}: must be an object.`);
          continue;
        }
        for (const [key, value] of Object.entries(valuesRaw)) {
          const field = fieldsByPath.get(`${group}.${key}`);
          if (!field) {
            errors.push(`${transportLabel}: unknown parameter '${group}.${key}'.`);
            continue;
          }
          const where = `${transportLabel} · ${field.label}`;
          let out: number | boolean;
          if (field.valueType === "boolean") {
            if (typeof value !== "boolean" && value !== 0 && value !== 1) {
              errors.push(`${where}: must be true or false.`);
              continue;
            }
            out = Boolean(value);
          } else if (typeof value !== "number" || !Number.isFinite(value)) {
            errors.push(`${where}: must be a number.`);
            continue;
          } else if (field.valueType === "integer" && !Number.isInteger(value)) {
            errors.push(`${where}: must be a whole number.`);
            continue;
          } else if (value < field.min || value > field.max) {
            errors.push(`${where}: must be between ${fmtNum(field.min)} and ${fmtNum(field.max)} ${field.unit}.`);
            continue;
          } else {
            out = value;
          }
          const transportOut = (normalized[transport as TurnDetectionTransport] ??= {});
          const groupOut = (transportOut[group as TurnDetectionGroup] ??= {});
          groupOut[key] = out;
        }
      }
    }
  }

  if (errors.length > 0) return fail(errors);

  const mode = raw.mode as TurnDetectionMode;
  const warnings: string[] = [];
  if (mode !== "custom" && Object.keys(normalized).length > 0) {
    warnings.push(`Mode is "${mode}", so the overrides in this document are ignored — only Custom mode applies overrides.`);
  }
  return {
    errors: [],
    warnings,
    document: { mode, overrides: mode === "custom" ? normalized : {} },
  };
}
