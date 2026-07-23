import { useEffect, useState } from "react";
import type { ProviderModelInfo, VoiceCapability } from "@/types/domain";
import { getProviderCatalog, listProviderModels } from "@/services/api";

/* Provider → model dependent selection, shared by every AI-configuration form
   (Platform Configuration AI profiles, and reusable by voice/tenant config).
   Options come from the DB-driven provider catalog — never hardcoded — and the
   backend validates the same catalog, so this UI is advisory, not the gate. */

export interface ProviderOption {
  code: string;
  name: string;
}

export interface ModelOption {
  code: string;
  displayName: string;
  isDefault: boolean;
}

/* Module-level caches: the catalog is small and stable within a session, and
   several fields on one form share the same capability. */
const providerCache = new Map<string, Promise<ProviderOption[]>>();
const modelCache = new Map<string, Promise<ProviderModelInfo[]>>();

function fetchProviders(capability: VoiceCapability): Promise<ProviderOption[]> {
  let cached = providerCache.get(capability);
  if (!cached) {
    cached = getProviderCatalog(capability).then((catalog) =>
      (catalog[capability] ?? []).map((p) => ({ code: p.code, name: p.name })));
    providerCache.set(capability, cached);
    cached.catch(() => providerCache.delete(capability)); // allow retry after failure
  }
  return cached;
}

function fetchModels(capability: VoiceCapability, provider: string): Promise<ProviderModelInfo[]> {
  const key = `${capability}:${provider}`;
  let cached = modelCache.get(key);
  if (!cached) {
    cached = listProviderModels(capability, provider);
    modelCache.set(key, cached);
    cached.catch(() => modelCache.delete(key));
  }
  return cached;
}

/** Test hook: drop all cached catalog responses. */
export function clearProviderCatalogCache() {
  providerCache.clear();
  modelCache.clear();
}

export function useProviderOptions(capability: VoiceCapability) {
  const [providers, setProviders] = useState<ProviderOption[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setProviders(null);
    setError(null);
    fetchProviders(capability)
      .then((p) => { if (alive) setProviders(p); })
      .catch((e: Error) => { if (alive) { setError(e.message); setProviders([]); } });
    return () => { alive = false; };
  }, [capability]);
  return { providers, loading: providers === null && !error, error };
}

/** Full model records (incl. paramsSchema and languages) for provider-aware forms. */
export function useModelInfos(capability: VoiceCapability, provider: string | null | undefined) {
  const [models, setModels] = useState<ProviderModelInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setError(null);
    if (!provider) {
      setModels([]);
      return () => { alive = false; };
    }
    setModels(null);
    fetchModels(capability, provider)
      .then((m) => { if (alive) setModels(m); })
      .catch((e: Error) => { if (alive) { setError(e.message); setModels([]); } });
    return () => { alive = false; };
  }, [capability, provider]);
  return { models, loading: provider ? models === null && !error : false, error };
}

export function useModelOptions(capability: VoiceCapability, provider: string | null | undefined) {
  const { models, loading, error } = useModelInfos(capability, provider);
  return {
    models: models === null ? null
      : models.map((m): ModelOption => ({ code: m.code, displayName: m.displayName, isDefault: m.isDefault })),
    loading,
    error,
  };
}

export function ProviderSelect({ capability, value, onChange, disabled, label }: {
  capability: VoiceCapability;
  value: string;
  onChange: (code: string) => void;
  disabled?: boolean;
  label?: string;
}) {
  const { providers, loading, error } = useProviderOptions(capability);
  const known = (providers ?? []).some((p) => p.code === value);
  return (
    <select
      className="select"
      value={value}
      disabled={disabled || loading}
      aria-label={label ?? `${capability.toUpperCase()} provider`}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{loading ? "Loading providers…" : "—"}</option>
      {/* Keep an inactive/removed saved value visible in edit mode with a
          warning; it is never offered as a normal option and the backend
          rejects it on save. */}
      {value && !loading && !known && <option value={value}>{value} (unavailable — inactive)</option>}
      {(providers ?? []).map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
      {error && <option value="" disabled>Failed to load providers</option>}
    </select>
  );
}

export function ModelSelect({ capability, provider, value, onChange, disabled, label }: {
  capability: VoiceCapability;
  /** Selected provider code; the model list belongs to this provider. */
  provider: string;
  value: string;
  onChange: (code: string) => void;
  disabled?: boolean;
  label?: string;
}) {
  const { models, loading, error } = useModelOptions(capability, provider || null);
  const known = (models ?? []).some((m) => m.code === value);
  const empty = Boolean(provider) && !loading && !error && (models ?? []).length === 0;
  return (
    <select
      className="select"
      value={value}
      disabled={disabled || !provider || loading || empty}
      aria-label={label ?? `${capability.toUpperCase()} model`}
      title={!provider ? "Select a provider first" : empty ? "This provider has no configured models" : undefined}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">
        {!provider ? "Select a provider first"
          : loading ? "Loading models…"
          : error ? "Failed to load models"
          : empty ? "No models configured for this provider"
          : "—"}
      </option>
      {/* Keep an inactive/removed saved value visible in edit mode with a
          warning; the backend rejects it on save. */}
      {value && provider && !loading && !known && <option value={value}>{value} (unavailable — inactive)</option>}
      {(models ?? []).map((m) => (
        <option key={m.code} value={m.code}>{m.displayName}{m.isDefault ? " · default" : ""}</option>
      ))}
    </select>
  );
}
