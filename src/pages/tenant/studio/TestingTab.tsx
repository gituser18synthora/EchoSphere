import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { TraceStep, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listPrompts, listScenarios, runSuite as runSuiteApi, testBotChat } from "@/services/api";
import { VoiceClient, type VoiceSessionConfig } from "@/services/voiceClient";
import { Button, CardSkeleton, ErrorState, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";

/* Text testing runs each turn through the REAL runtime stack on the backend
   (TurnRouter routing + the WorkflowEngine executing the bot's saved
   workflow), so what you see here is what a live call does — only the
   audio layer (STT/TTS) is out of the loop. */

/* Readable names for the live-session chips — never raw locale/provider codes. */
const LANGUAGE_NAMES: Record<string, string> = {
  hi: "Hindi", en: "English", bn: "Bengali", ta: "Tamil", te: "Telugu",
  mr: "Marathi", gu: "Gujarati", kn: "Kannada", ml: "Malayalam",
  pa: "Punjabi", or: "Odia", ur: "Urdu",
};
export function languageName(locale?: string): string {
  if (!locale) return "";
  return LANGUAGE_NAMES[locale.split("-")[0].toLowerCase()] ?? locale;
}
const PROVIDER_NAMES: Record<string, string> = {
  sarvam: "Sarvam", elevenlabs: "ElevenLabs", openai: "OpenAI",
  azure: "Azure", google: "Google", mock: "Mock (dev)",
};
export function providerName(code?: string): string {
  if (!code) return "";
  return PROVIDER_NAMES[code.toLowerCase()] ?? code;
}
/** Voice shown for the current conversation language (per-language voice
    first, then the bot's configured default voice). */
export function activeVoice(
  config: VoiceSessionConfig | null,
  language?: string,
): { provider: string; voice: string } | null {
  if (!config) return null;
  const forLanguage = language ? config.voices?.[language] : undefined;
  return forLanguage ?? config.defaultVoice ?? null;
}

export default function TestingTab({ bot }: { bot: VoiceBot }) {
  const scenariosQ = useAsync(() => listScenarios(bot.id), [bot.id]);
  const promptsQ = useAsync(() => listPrompts(bot.id), [bot.id]);
  const navigate = useNavigate();
  const { toast } = useApp();
  const greetingPrompt = promptsQ.data?.find((p) => p.type === "greeting");
  const greetingText =
    greetingPrompt?.versions.find((v) => v.version === greetingPrompt.activeVersion)?.variants[0]?.content
    ?? `Test session for ${bot.name} — type a caller message; it runs through the real routing and workflow engine.`;
  const [steps, setSteps] = useState<TraceStep[]>([]);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null);
  const [runningSuite, setRunningSuite] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  /* ---------- Live voice session ---------- */
  const voiceRef = useRef<VoiceClient | null>(null);
  const [voiceActive, setVoiceActive] = useState(false);
  const [voiceConnecting, setVoiceConnecting] = useState(false);
  const [voiceStatus, setVoiceStatus] = useState<"listening" | "bot_speaking">("listening");
  const [sessionConfig, setSessionConfig] = useState<VoiceSessionConfig | null>(null);
  const [liveLanguage, setLiveLanguage] = useState<string>("");

  /* Tear the session down when leaving the tab */
  useEffect(() => () => { voiceRef.current?.stop(); voiceRef.current = null; }, []);

  const scrollToEnd = () =>
    setTimeout(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);

  const appendVoiceStep = (speaker: "user" | "bot", text: string) => {
    setSteps((s) => [...s, { turn: s.length + 2, speaker, text }]);
    scrollToEnd();
  };

  const stopVoice = () => {
    const client = voiceRef.current;
    voiceRef.current = null;
    client?.stop();
    setVoiceActive(false);
    setVoiceStatus("listening");
    setSessionConfig(null);
    setLiveLanguage("");
  };

  const startVoice = async () => {
    if (voiceConnecting || voiceActive) return;
    setVoiceConnecting(true);
    const client = new VoiceClient({
      onSessionConfig: (config) => {
        setSessionConfig(config);
        setLiveLanguage(config.language ?? "");
      },
      onTranscript: (text) => appendVoiceStep("user", text),
      onBotText: (text) => appendVoiceStep("bot", text),
      onLanguage: (locale) => setLiveLanguage(locale),
      onEvent: (name, detail) => {
        if (name === "bot_speaking_started") setVoiceStatus("bot_speaking");
        else if (name === "bot_speaking_stopped" || name === "interruption") setVoiceStatus("listening");
        else if (name === "language_unsupported") {
          const supported = voiceRef.current?.sessionConfig?.languages ?? [];
          toast(
            `Only ${supported.map(languageName).join(" and ") || "the configured languages"} are supported` +
              (detail?.language ? ` — heard ${languageName(String(detail.language))}` : ""),
            "info",
          );
        }
      },
      onClose: () => {
        if (voiceRef.current) {
          voiceRef.current = null;
          setVoiceActive(false);
          setVoiceStatus("listening");
          toast("Voice session ended", "info");
        }
      },
      onError: (message) => toast(message, "error"),
    });
    try {
      await client.start(bot.id);
      voiceRef.current = client;
      setVoiceActive(true);
      setVoiceStatus("listening");
      toast("Voice session live — speak into your microphone");
    } catch (e) {
      client.stop();
      toast(e instanceof Error ? e.message : "Could not start the voice session", "error");
    } finally {
      setVoiceConnecting(false);
    }
  };

  /* One conversation per tab mount — the backend keeps workflow state per id. */
  const chatSessionRef = useRef<string | undefined>(undefined);

  const send = async () => {
    const text = input.trim();
    if (!text || thinking) return;
    const userStep: TraceStep = { turn: steps.length + 2, speaker: "user", text };
    setSteps((s) => [...s, userStep]);
    setInput("");
    setThinking(true);
    scrollToEnd();
    const started = performance.now();
    try {
      const result = await testBotChat(bot.id, text, chatSessionRef.current);
      chatSessionRef.current = result.sessionId;
      const latency = Math.round(performance.now() - started);
      setSteps((s) => {
        const reply: TraceStep = {
          turn: s.length + 2,
          speaker: "bot",
          text: result.reply,
          intent: result.matchedIntent ?? undefined,
          confidence: result.matchedIntent ? result.confidence : undefined,
          route: result.route,
          workflowName: result.workflow?.name,
          workflowNodes: result.workflow?.nodeTrace,
          workflowSlots: result.workflow?.slots,
          workflowDone: result.workflow?.done,
          latencyMs: latency,
        };
        setSelectedTurn(reply.turn);
        return [...s, reply];
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Test turn failed", "error");
      setSteps((s) => [...s, {
        turn: s.length + 2, speaker: "bot",
        text: "The test turn failed — check that the platform API is running and try again.",
      }]);
    } finally {
      setThinking(false);
      scrollToEnd();
    }
  };

  const greetingStep: TraceStep = {
    turn: 1,
    speaker: "bot",
    text: greetingText,
    promptVersion: greetingPrompt ? `greeting v${greetingPrompt.activeVersion}` : undefined,
    latencyMs: 400,
    costUsd: 0.004,
  };
  const allSteps = [greetingStep, ...steps];

  const selected = allSteps.find((s) => s.turn === selectedTurn && s.speaker === "bot") ?? [...allSteps].reverse().find((s) => s.speaker === "bot");
  const failing = (scenariosQ.data ?? []).filter((s) => s.lastRun && !s.lastRun.pass);

  const runSuite = async () => {
    setRunningSuite(true);
    try {
      const result = await runSuiteApi(bot.id);
      toast(`Regression suite finished: ${result.passed} passed, ${result.failed} failed — details below`);
      scenariosQ.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Suite run failed", "error");
    } finally {
      setRunningSuite(false);
    }
  };

  return (
    <div className="col gap-16">
      <div className="grid" style={{ gridTemplateColumns: "1.3fr 1fr", gap: 16, alignItems: "stretch" }}>
        {/* Simulator */}
        <div className="card col" style={{ height: 480 }}>
          <div className="card-header" style={{ padding: "12px 16px" }}>
            <div className="row gap-8">
              <span className="card-title">Simulator</span>
              <span className="tag">draft {bot.version}</span>
            </div>
            <div className="row gap-6">
              {voiceActive && (
                <span className={`chip ${voiceStatus === "bot_speaking" ? "chip-brand" : "chip-good"}`}>
                  <span className="chip-dot live" />
                  {voiceStatus === "bot_speaking" ? "Bot speaking" : "Listening"}
                </span>
              )}
              <Button size="sm" variant="ghost" icon="refresh" onClick={() => { setSteps([]); setSelectedTurn(null); }}>Reset</Button>
              {voiceActive ? (
                <Button size="sm" variant="danger-ghost" icon="x" onClick={stopVoice}>Stop voice session</Button>
              ) : (
                <Button size="sm" variant="ghost" icon="mic" busy={voiceConnecting} onClick={() => void startVoice()}>Voice mode</Button>
              )}
            </div>
          </div>
          {voiceActive && sessionConfig && (
            <div
              className="row gap-6"
              style={{ padding: "6px 16px", borderBottom: "1px solid var(--hairline)", flexWrap: "wrap" }}
              data-testid="live-session-status"
            >
              {liveLanguage && <span className="tag">{languageName(liveLanguage)}</span>}
              {(() => {
                const voice = activeVoice(sessionConfig, liveLanguage);
                return voice ? (
                  <span className="tag">{providerName(voice.provider)} · {voice.voice}</span>
                ) : null;
              })()}
              {Object.keys(sessionConfig.warnings ?? {}).map((locale) => (
                <span key={locale} className="chip chip-warning" role="alert">
                  No compatible {languageName(locale)} voice is configured for this Voice Bot.
                </span>
              ))}
            </div>
          )}
          <div ref={listRef} className="col grow" style={{ padding: 16, gap: 10, overflowY: "auto" }}>
            {allSteps.map((s) => (
              <button
                key={s.turn}
                className={`transcript-bubble ${s.speaker}`}
                style={{ cursor: s.speaker === "bot" ? "pointer" : "default", textAlign: "left", outline: selectedTurn === s.turn && s.speaker === "bot" ? "2px solid var(--brand-400)" : "none" }}
                onClick={() => s.speaker === "bot" && setSelectedTurn(s.turn)}
                aria-label={s.speaker === "bot" ? `Inspect turn ${s.turn}` : undefined}
              >
                {s.text}
              </button>
            ))}
            {thinking && (
              <div className="transcript-bubble bot row gap-8"><span className="spinner" /> thinking…</div>
            )}
          </div>
          <div className="row gap-8" style={{ padding: "12px 16px", borderTop: "1px solid var(--hairline)" }}>
            <input
              className="input"
              placeholder='Try: "I need to see a doctor Thursday" or "do you take Aetna?"'
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              aria-label="Simulator input"
            />
            <Button variant="primary" icon="send" onClick={send} disabled={thinking || !input.trim()}>Send</Button>
          </div>
        </div>

        {/* Execution trace */}
        <div className="card col" style={{ height: 480 }}>
          <div className="card-header" style={{ padding: "12px 16px" }}>
            <span className="card-title">Execution trace</span>
            {selected && <span className="tag t-num">turn {selected.turn}</span>}
          </div>
          <div className="col grow" style={{ padding: 16, gap: 10, overflowY: "auto" }}>
            {!selected && <p className="t-sub">Send a message, then click any bot reply to inspect how it was produced.</p>}
            {selected && (
              <>
                <TraceRow icon="target" label="Intent">
                  {selected.intent ? (
                    <span className="row gap-8">
                      <code>{selected.intent}</code>
                      <span className={`t-num t-strong ${selected.confidence && selected.confidence < 0.7 ? "t-bad" : "t-good"}`}>
                        {selected.confidence ? `${(selected.confidence * 100).toFixed(0)}%` : ""}
                      </span>
                      {selected.confidence && selected.confidence < 0.7 && (
                        <Button size="sm" variant="ghost" onClick={() => navigate(`/t/bots/${bot.id}/intents`)}>Fix intent →</Button>
                      )}
                    </span>
                  ) : <span className="t-micro">n/a (scripted prompt)</span>}
                </TraceRow>
                <TraceRow icon="workflow" label="Workflow">
                  {selected.workflowName ? (
                    <div className="col gap-4" style={{ fontSize: 12 }}>
                      <span className="row gap-6">
                        <code>{selected.workflowName}</code>
                        <span className={`chip ${selected.workflowDone ? "chip-neutral" : "chip-good"}`}>
                          {selected.workflowDone ? "finished" : "in progress"}
                        </span>
                      </span>
                      {selected.workflowNodes && selected.workflowNodes.length > 0 && (
                        <span className="t-micro" data-testid="workflow-node-trace">
                          nodes: {selected.workflowNodes.join(" → ")}
                        </span>
                      )}
                      {selected.workflowSlots && Object.keys(selected.workflowSlots).length > 0 && (
                        <span className="t-micro" data-testid="workflow-slots">
                          collected: {Object.entries(selected.workflowSlots)
                            .map(([k, v]) => `${k}=${String(v)}`).join(", ")}
                        </span>
                      )}
                    </div>
                  ) : (
                    <span className="t-micro">{selected.route ? `route: ${selected.route}` : "not in a workflow"}</span>
                  )}
                </TraceRow>
                <TraceRow icon="book" label="Knowledge chunks">
                  {selected.chunksUsed?.length ? (
                    <div className="col gap-4">
                      {selected.chunksUsed.map((c) => (
                        <span key={c} className="row gap-6" style={{ fontSize: 12 }}>
                          <code style={{ background: "var(--surface-3)", padding: "2px 6px", borderRadius: 5 }}>{c}</code>
                          {c.includes("stale") && (
                            <Button size="sm" variant="ghost" onClick={() => navigate(`/t/bots/${bot.id}/knowledge`)}>Re-sync →</Button>
                          )}
                        </span>
                      ))}
                    </div>
                  ) : <span className="t-micro">none used</span>}
                </TraceRow>
                <TraceRow icon="zap" label="API calls">
                  {selected.apiCalls?.length ? selected.apiCalls.map((a) => (
                    <span key={a.name} className="row gap-6" style={{ fontSize: 12 }}>
                      <Icon name={a.ok ? "check-circle" : "x-circle"} size={13} style={{ color: a.ok ? "var(--status-good)" : "var(--status-critical)" }} />
                      {a.name} <span className="t-micro t-num">{a.ms}ms</span>
                    </span>
                  )) : <span className="t-micro">none</span>}
                </TraceRow>
                <TraceRow icon="message" label="Prompt version">
                  {selected.promptVersion ? (
                    <span className="row gap-8">
                      <code>{selected.promptVersion}</code>
                      <Button size="sm" variant="ghost" onClick={() => navigate(`/t/bots/${bot.id}/prompts`)}>Open →</Button>
                    </span>
                  ) : <span className="t-micro">generated response</span>}
                </TraceRow>
                <TraceRow icon="clock" label="Latency">
                  <span className="t-num t-strong">{selected.latencyMs != null ? `${selected.latencyMs}ms` : "—"}</span>
                </TraceRow>
                {flags.tenantCostVisibility && (
                  <TraceRow icon="dollar" label="Turn cost">
                    <span className="t-num t-strong">{selected.costUsd != null ? `$${selected.costUsd.toFixed(4)}` : "—"}</span>
                  </TraceRow>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      {/* Scenarios & regression suite */}
      <div className="card">
        <div className="card-header">
          <div className="col gap-2">
            <span className="card-title">Regression suite</span>
            <span className="t-micro">
              {scenariosQ.data ? `${scenariosQ.data.filter((s) => s.lastRun?.pass).length} passing · ${failing.length} failing · ${scenariosQ.data.filter((s) => !s.lastRun).length} never run` : "Loading…"}
            </span>
          </div>
          <div className="row gap-6">
            <Button size="sm" icon="plus" onClick={() => toast("Scenario recorder started — interact with the simulator, then save as a scenario", "info")}>New scenario</Button>
            <Button size="sm" variant="primary" icon="play" busy={runningSuite} onClick={runSuite}>Run all</Button>
          </div>
        </div>
        {scenariosQ.error && <ErrorState message={scenariosQ.error} onRetry={scenariosQ.reload} />}
        {scenariosQ.loading && <div style={{ padding: 16 }}><CardSkeleton rows={5} /></div>}
        {scenariosQ.data && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Scenario</th><th>Suite</th><th className="num">Steps</th><th>Last run</th><th>Result</th><th></th></tr>
              </thead>
              <tbody>
                {scenariosQ.data.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <div className="t-strong">{s.name}</div>
                      {s.lastRun && !s.lastRun.pass && <div className="t-micro" style={{ color: "var(--status-critical)", maxWidth: 380 }}>Step {s.lastRun.failedStep}: {s.lastRun.reason}</div>}
                    </td>
                    <td><span className="tag">{s.suite}</span></td>
                    <td className="num t-num">{s.steps}</td>
                    <td className="t-sub">{s.lastRun ? new Date(s.lastRun.at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—"}</td>
                    <td>{s.lastRun ? <StatusChip status={s.lastRun.pass ? "good" : "failed"} label={s.lastRun.pass ? "Pass" : "Fail"} /> : <StatusChip status="pending" label="Not run" />}</td>
                    <td>
                      {s.lastRun && !s.lastRun.pass && (
                        <Button size="sm" variant="ghost" onClick={() =>
                          navigate(`/t/bots/${bot.id}/${s.lastRun!.reason?.includes("stale") ? "knowledge" : "intents"}`)
                        }>
                          Fix →
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function TraceRow({ icon, label, children }: { icon: Parameters<typeof Icon>[0]["name"]; label: string; children: React.ReactNode }) {
  return (
    <div className="col gap-4" style={{ padding: "8px 10px", background: "var(--surface-2)", borderRadius: 10 }}>
      <span className="t-micro row gap-4" style={{ fontWeight: 650 }}><Icon name={icon} size={12} />{label}</span>
      <div>{children}</div>
    </div>
  );
}
