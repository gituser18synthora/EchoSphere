import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { HumanSpeechSettingsEditor, validateHumanSpeechOverrides } from "@/components/HumanSpeechSettings";
import FillerAudioLibrary from "./FillerAudioLibrary";
import { Button, Callout, CardSkeleton, ErrorState, StatusChip } from "@/components/ui";
import { getVoiceSettings, saveVoiceSettings } from "@/services/api";
import type { ApiRequestError } from "@/services/http";
import { useApp } from "@/state/AppContext";
import type { HumanSpeechSettings, VoiceBot, VoiceSettings } from "@/types/domain";

interface Props {
  bot: VoiceBot;
  onDirtyChange?: (dirty: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  onNavigate?: (tab: string) => void;
}

const inheritanceError = "Natural conversation settings could not load their inherited values. Try again before editing.";

function hasInheritance(settings: VoiceSettings): boolean {
  return !!settings.humanSpeech
    && !!settings.humanSpeechInherited
    && Object.keys(settings.humanSpeechInherited).length > 0
    && !!settings.humanSpeechInheritedSources
    && Object.keys(settings.humanSpeechInheritedSources).length > 0;
}

/** Structural equality: selection values are nested objects, so identity is not enough. */
function sameValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (typeof left !== "object" || typeof right !== "object" || left === null || right === null) return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const leftKeys = Object.keys(left as object);
  const rightKeys = Object.keys(right as object);
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key) => Object.prototype.hasOwnProperty.call(right, key)
      && sameValue((left as Record<string, unknown>)[key], (right as Record<string, unknown>)[key]));
}

function sameOverrides(left: HumanSpeechSettings, right: HumanSpeechSettings): boolean {
  const entries = Object.entries(left);
  return entries.length === Object.keys(right).length
    && entries.every(([key, value]) => Object.prototype.hasOwnProperty.call(right, key)
      && sameValue(value, right[key as keyof HumanSpeechSettings]));
}

export default function NaturalConversationTab({ bot, onDirtyChange, onSavingChange, onNavigate }: Props) {
  const navigate = useNavigate();
  const { toast, hasPermission } = useApp();
  const canManage = hasPermission("manage_voices") || hasPermission("bots.manage");
  const [saved, setSaved] = useState<VoiceSettings | null>(null);
  const [override, setOverride] = useState<HumanSpeechSettings>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [savedMessage, setSavedMessage] = useState(false);
  const requestVersion = useRef(0);
  const dirty = !!saved && !sameOverrides(override, saved.humanSpeech);

  useEffect(() => {
    const version = ++requestVersion.current;
    setLoading(true);
    setLoadError(null);
    setSaved(null);
    setOverride({});
    setSaving(false);
    setErrors([]);
    setWarnings([]);
    setSavedMessage(false);
    void getVoiceSettings(bot.id).then((settings) => {
      if (version !== requestVersion.current) return;
      if (!hasInheritance(settings)) {
        setLoadError(inheritanceError);
      } else {
        setSaved(settings);
        setOverride({ ...settings.humanSpeech });
      }
      setLoading(false);
    }).catch((error: unknown) => {
      if (version !== requestVersion.current) return;
      setLoadError(error instanceof Error ? error.message : "Could not load natural conversation settings.");
      setLoading(false);
    });
    return () => { requestVersion.current += 1; };
  }, [bot.id, retry]);

  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => { onDirtyChange?.(false); }, [onDirtyChange]);
  useEffect(() => { onSavingChange?.(saving); }, [saving, onSavingChange]);
  useEffect(() => () => { onSavingChange?.(false); }, [onSavingChange]);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const clearFeedback = () => {
    setErrors([]);
    setWarnings([]);
    setSavedMessage(false);
  };

  const save = async () => {
    if (!canManage || saving || !dirty) return;
    clearFeedback();
    const localErrors = validateHumanSpeechOverrides(override);
    if (localErrors.length) {
      setErrors(localErrors);
      return;
    }
    const version = requestVersion.current;
    setSaving(true);
    try {
      // Save sparse bot overrides only. Copying effective values would break
      // inheritance; sending provider fields could overwrite another tab.
      const result = await saveVoiceSettings(bot.id, { humanSpeech: { ...override } });
      if (version !== requestVersion.current) return;
      if (!hasInheritance(result.settings)) {
        setLoadError(inheritanceError);
        return;
      }
      setSaved(result.settings);
      setOverride({ ...result.settings.humanSpeech });
      setWarnings(result.warnings);
      setSavedMessage(true);
      toast(result.warnings.length ? "Natural conversation settings saved with warnings" : "Natural conversation settings saved",
        result.warnings.length ? "info" : undefined);
    } catch (error) {
      if (version !== requestVersion.current) return;
      const err = error as ApiRequestError;
      setErrors(err.errors?.length ? err.errors : [err.message || "Could not save natural conversation settings."]);
      toast("Could not save natural conversation settings", "error");
    } finally {
      if (version === requestVersion.current) setSaving(false);
    }
  };

  const openTab = (tab: string) => {
    if (onNavigate) {
      onNavigate(tab);
    } else if (!dirty || window.confirm("Discard unsaved natural conversation changes?")) {
      navigate(`/t/bots/${bot.id}/${tab}`);
    }
  };

  if (loading) return <CardSkeleton rows={8} />;
  if (loadError) return <ErrorState message={loadError} onRetry={() => setRetry((value) => value + 1)} />;
  if (!saved) return null;

  return (
    <div className="col gap-16">
      <div className="row-between gap-12" style={{ flexWrap: "wrap" }}>
        <div>
          <h2 className="t-section" style={{ margin: 0 }}>Natural Conversation</h2>
          <p className="t-sub mt-4">Control fillers, acknowledgements, backchannels and natural pauses for this bot.</p>
          <p className="t-micro">Speaking speed and the base sentence pause are in Voice settings. Some effects depend on the selected voice provider.</p>
        </div>
        <div className="row gap-8">
          <Button onClick={() => openTab("voice")} disabled={saving}>Voice settings</Button>
          <Button icon="play" onClick={() => openTab("testing")} disabled={saving}>Test call</Button>
        </div>
      </div>

      <Callout tone="info">
        Saved changes apply to new calls for published bots. Calls already in progress keep their current settings.
        {" "}Save before starting a test call.
      </Callout>
      {!canManage && <Callout tone="info">You can view these settings. Editing requires the manage_voices or bots.manage permission.</Callout>}

      <section className="card card-pad">
        <HumanSpeechSettingsEditor
          scope="bot"
          override={override}
          inherited={saved.humanSpeechInherited}
          inheritedSources={saved.humanSpeechInheritedSources}
          collapseAdvanced
          disabled={!canManage || saving}
          onChange={(next) => { setOverride(next); clearFeedback(); }}
        />
      </section>

      <FillerAudioLibrary
        botId={bot.id}
        override={override}
        inherited={saved.humanSpeechInherited}
        disabled={!canManage || saving}
        onChange={(next) => { setOverride(next); clearFeedback(); }}
      />

      {errors.length > 0 && (
        <div role="alert">
          <Callout tone="critical" title="Could not save natural conversation settings">
            <ul style={{ margin: 0, paddingLeft: 16 }}>{errors.map((error) => <li key={error}>{error}</li>)}</ul>
          </Callout>
        </div>
      )}
      {warnings.length > 0 && (
        <Callout tone="warning" title="Saved with warnings">
          <ul style={{ margin: 0, paddingLeft: 16 }}>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </Callout>
      )}
      <div className="row-between gap-12" style={{ flexWrap: "wrap" }}>
        <span role="status">
          {dirty ? <StatusChip status="warning" label="Unsaved changes" />
            : savedMessage ? "Natural conversation settings saved" : "No unsaved changes"}
        </span>
        <div className="row gap-8">
          <Button disabled={!canManage || saving || !dirty} onClick={() => {
            setOverride({ ...saved.humanSpeech });
            clearFeedback();
          }}>Discard changes</Button>
          <Button variant="primary" icon="check" busy={saving} disabled={!canManage || saving || !dirty}
            onClick={() => void save()}>Save natural conversation settings</Button>
        </div>
      </div>
    </div>
  );
}
