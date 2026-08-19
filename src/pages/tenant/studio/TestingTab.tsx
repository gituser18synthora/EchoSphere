import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import type { Prompt, SimulateTrace, TraceStep, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listPrompts, listScenarios, runSuite as runSuiteApi, simulateTurn, testBotChat } from "@/services/api";
import { VoiceClient, type VoiceSessionConfig } from "@/services/voiceClient";
import { Button, Callout, CardSkeleton, ErrorState, Field, StatusChip, Toggle } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { JsonView } from "@/components/JsonView";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";
import { formatChatTime, nowWithMicroseconds } from "@/services/chatTime";

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
): { provider: string; voice: string; gender?: string } | null {
  if (!config) return null;
  const forLanguage = language ? config.voices?.[language] : undefined;
  return forLanguage ?? config.defaultVoice ?? null;
}

export default function TestingTab({ bot }: { bot: VoiceBot }) {
  const scenariosQ = useAsync(() => listScenarios(bot.id), [bot.id]);
  const promptsQ = useAsync(() => listPrompts(bot.id), [bot.id]);
  const navigate = useNavigate();
  const { toast, hasPermission } = useApp();
  const showCosts = flags.tenantCostVisibility && hasPermission("costs.view");
  const greetingPrompt = promptsQ.data?.find((p) => p.type === "greeting");
  const greetingVersion = greetingPrompt
    ? (greetingPrompt.publishedVersion ?? greetingPrompt.activeVersion)
    : undefined;
  const greetingVariant =
    greetingPrompt?.versions.find((v) => v.version === greetingVersion)?.variants[0];
  const greetingText =
    greetingVariant?.content
    ?? `Test session for ${bot.name} — type a caller message; it runs through the real routing and workflow engine.`;
  const [steps, setSteps] = useState<TraceStep[]>([]);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [chatEnded, setChatEnded] = useState(false);
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null);
  const [runningSuite, setRunningSuite] = useState(false);
  const [greetingAt, setGreetingAt] = useState(nowWithMicroseconds);
  const listRef = useRef<HTMLDivElement>(null);

  /* ---------- Live voice session ---------- */
  const voiceRef = useRef<VoiceClient | null>(null);
  const awaitingVoiceGreetingRef = useRef(false);
  const [voiceActive, setVoiceActive] = useState(false);
  const [voiceConnecting, setVoiceConnecting] = useState(false);
  const [voiceStatus, setVoiceStatus] = useState<"listening" | "bot_speaking">("listening");
  const [sessionConfig, setSessionConfig] = useState<VoiceSessionConfig | null>(null);
  const [liveLanguage, setLiveLanguage] = useState<string>("");

  /* Tear the session down when leaving the tab */
  useEffect(() => () => { voiceRef.current?.stop(); voiceRef.current = null; }, []);

  const scrollToEnd = () =>
    setTimeout(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);

  const appendVoiceStep = (speaker: "user" | "bot", text: string, at?: string) => {
    setSteps((s) => [...s, {
      turn: s.length + 2, speaker, text, at: at ?? nowWithMicroseconds(),
    }]);
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
      onTranscript: (text, at) => appendVoiceStep("user", text, at),
      onBotText: (text, at) => {
        // The greeting is already rendered as turn 1. Replace its local mount
        // time with the runtime's stored turn time instead of duplicating it.
        if (awaitingVoiceGreetingRef.current) {
          awaitingVoiceGreetingRef.current = false;
          setGreetingAt(at ?? nowWithMicroseconds());
          return;
        }
        appendVoiceStep("bot", text, at);
      },
      onTurnRewound: (userText, botText) => {
        // The runtime merged a straggler final (or clarified fragment) into
        // one turn and popped these entries from its own transcript; mirror
        // that here or the merged turn repeats the fragment's words.
        setSteps((s) => {
          const next = [...s];
          const dropTrailing = (speaker: "user" | "bot", text?: string) => {
            const last = next[next.length - 1];
            if (text && last?.speaker === speaker && last.text === text) next.pop();
          };
          dropTrailing("bot", botText);
          dropTrailing("user", userText);
          return next;
        });
      },
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
      awaitingVoiceGreetingRef.current = true;
      await client.start(bot.id);
      voiceRef.current = client;
      setVoiceActive(true);
      setVoiceStatus("listening");
      toast("Voice session live — speak into your microphone");
    } catch (e) {
      awaitingVoiceGreetingRef.current = false;
      client.stop();
      toast(e instanceof Error ? e.message : "Could not start the voice session", "error");
    } finally {
      setVoiceConnecting(false);
    }
  };

  /* One conversation per tab mount — the backend keeps workflow state per id. */
  const chatSessionRef = useRef<string | undefined>(undefined);
  const chatLanguageRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (!steps.length && greetingVariant?.language) {
      chatLanguageRef.current = greetingVariant.language;
    }
  }, [greetingVariant?.language, steps.length]);

  const send = async () => {
    const text = input.trim();
    if (!text || thinking || chatEnded) return;
    const userStep: TraceStep = {
      turn: steps.length + 2, speaker: "user", text, at: nowWithMicroseconds(),
    };
    setSteps((s) => [...s, userStep]);
    setInput("");
    setThinking(true);
    scrollToEnd();
    const started = performance.now();
    try {
      const history = [greetingStep, ...steps].map((step) => ({
        role: step.speaker === "bot" ? "assistant" as const : "user" as const,
        content: step.text,
      }));
      const result = await testBotChat(
        bot.id,
        text,
        chatSessionRef.current,
        history,
        chatLanguageRef.current,
      );
      chatSessionRef.current = result.sessionId;
      chatLanguageRef.current = result.language;
      if (result.route === "handoff" || result.workflow?.status === "handoff") {
        setChatEnded(true);
      }
      const latency = result.latencyMs ?? Math.round(performance.now() - started);
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
          at: result.at ?? nowWithMicroseconds(),
        };
        setSelectedTurn(reply.turn);
        return [...s, reply];
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Test turn failed", "error");
      setSteps((s) => [...s, {
        turn: s.length + 2, speaker: "bot",
        text: "The test turn failed — check that the platform API is running and try again.",
        at: nowWithMicroseconds(),
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
    promptVersion: greetingPrompt && greetingVersion ? `greeting v${greetingVersion}` : undefined,
    latencyMs: 400,
    costUsd: 0.004,
    at: greetingAt,
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
              <Button size="sm" variant="ghost" icon="refresh" onClick={() => {
                setSteps([]);
                setSelectedTurn(null);
                setGreetingAt(nowWithMicroseconds());
                chatSessionRef.current = undefined;
                chatLanguageRef.current = greetingVariant?.language;
                setChatEnded(false);
              }}>Reset</Button>
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
                  <span className="tag">
                    {providerName(voice.provider)} · {voice.voice}
                    {voice.gender && voice.gender !== "neutral" ? ` · ${voice.gender}` : ""}
                  </span>
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
                <span className="transcript-text">{s.text}</span>
                {s.at && (
                  <time
                    className="transcript-bubble-time"
                    dateTime={s.at}
                    data-testid="message-timestamp"
                    title={s.at}
                  >
                    {formatChatTime(s.at)}
                  </time>
                )}
              </button>
            ))}
            {thinking && (
              <div className="transcript-bubble bot row gap-8"><span className="spinner" /> thinking…</div>
            )}
          </div>
          <div className="row gap-8" style={{ padding: "12px 16px", borderTop: "1px solid var(--hairline)" }}>
            <input
              className="input"
              placeholder={chatEnded
                ? "Call transferred — select Reset to start a new test call."
                : 'Try: "I need to see a doctor Thursday" or "do you take Aetna?"'}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              disabled={chatEnded}
              aria-label="Simulator input"
            />
            <Button variant="primary" icon="send" onClick={send} disabled={thinking || chatEnded || !input.trim()}>Send</Button>
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
                {showCosts && (
                  <TraceRow icon="dollar" label="Turn cost">
                    <span className="t-num t-strong">{selected.costUsd != null ? `$${selected.costUsd.toFixed(4)}` : "—"}</span>
                  </TraceRow>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      {/* Full-pipeline runtime simulator */}
      <RuntimeSimulator bot={bot} prompts={promptsQ.data ?? []} />

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

/* ============================================================
   Runtime simulator — one turn through the FULL pipeline
   (runtime context → prompt render → routing/intent → policy →
   mocked tools → workflow or LLM) with the complete trace.
   ============================================================ */

const simPreStyle: CSSProperties = {
  margin: 0, padding: 12, background: "var(--surface-2)", borderRadius: 10,
  fontSize: 12, lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word",
  maxHeight: 320, overflow: "auto",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
};

const chipValue = (v: unknown): string =>
  v !== null && typeof v === "object" ? JSON.stringify(v) : String(v);

interface RuntimeMessage {
  role: "user" | "assistant";
  content: string;
  at: string;
}

function RuntimeSimulator({ bot, prompts }: { bot: VoiceBot; prompts: Prompt[] }) {
  const { toast } = useApp();
  const [messages, setMessages] = useState<RuntimeMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [trace, setTrace] = useState<SimulateTrace | null>(null);
  const sessionRef = useRef<string | undefined>(undefined);

  const [promptId, setPromptId] = useState("");
  const [promptVersion, setPromptVersion] = useState("");
  const [contextSource, setContextSource] = useState<"saved" | "manual" | "api_mock" | "none">("saved");
  const [contextJson, setContextJson] = useState('{\n  "customer_name": "Rahul Sharma"\n}');
  const [language, setLanguage] = useState("");
  const [isFinal, setIsFinal] = useState(true);
  const [interrupted, setInterrupted] = useState(false);
  const [mockToolsJson, setMockToolsJson] = useState("{}");

  const selectedPrompt = prompts.find((p) => p.id === promptId);

  const parseJsonObject = (text: string, label: string): Record<string, unknown> | null => {
    if (!text.trim()) return {};
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
      return parsed as Record<string, unknown>;
    } catch {
      toast(`${label} must be a JSON object`, "error");
      return null;
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    let contextPayload: Record<string, unknown> | undefined;
    if (contextSource === "manual" || contextSource === "api_mock") {
      const parsed = parseJsonObject(contextJson, "Context payload");
      if (parsed === null) return;
      contextPayload = parsed;
    }
    const mockToolResults = parseJsonObject(mockToolsJson, "Mock tool results");
    if (mockToolResults === null) return;
    const sentAt = nowWithMicroseconds();
    setBusy(true);
    try {
      const result = await simulateTurn(bot.id, {
        message: text,
        messages,
        ...(promptId ? { promptId } : {}),
        ...(promptId && promptVersion ? { promptVersion: Number(promptVersion) } : {}),
        contextSource,
        ...(contextPayload !== undefined ? { contextPayload } : {}),
        ...(language ? { language } : {}),
        isFinal,
        interrupted,
        mockToolResults,
        ...(sessionRef.current ? { sessionId: sessionRef.current } : {}),
      });
      setTrace(result);
      if (result.sessionId) sessionRef.current = result.sessionId;
      if (!result.heldForFinal) {
        setMessages((m) => [
          ...m,
          { role: "user" as const, content: text, at: sentAt },
          ...(result.response ? [{
            role: "assistant" as const,
            content: result.response,
            at: nowWithMicroseconds(),
          }] : []),
        ]);
        setInput("");
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : "Simulation failed", "error");
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setMessages([]);
    setTrace(null);
    setInput("");
    sessionRef.current = undefined;
  };

  return (
    <details className="card">
      <summary className="card-header" style={{ cursor: "pointer", listStyle: "none" }}>
        <div className="col gap-2">
          <span className="card-title">Runtime simulator</span>
          <span className="t-micro">
            One turn through the full pipeline — runtime context, prompt render, intent, policy, mocked tools and workflow, with the complete trace.
          </span>
        </div>
        <Icon name="chevron-down" size={14} style={{ color: "var(--ink-3)", flexShrink: 0 }} />
      </summary>
      <div className="col gap-12" style={{ padding: 16, borderTop: "1px solid var(--hairline)" }}>
        <div className="grid grid-2" style={{ gap: 12 }}>
          <Field label="Prompt" hint="Which system prompt the simulated call runs on.">
            <select
              className="select" value={promptId} aria-label="Simulator prompt"
              onChange={(e) => { setPromptId(e.target.value); setPromptVersion(""); }}
            >
              <option value="">Published (live)</option>
              {prompts.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </Field>
          <Field label="Prompt version" hint={selectedPrompt ? "Any saved version — approval state is ignored here." : "Pick a prompt to choose a version."}>
            <select
              className="select" value={promptVersion} disabled={!selectedPrompt} aria-label="Simulator prompt version"
              onChange={(e) => setPromptVersion(e.target.value)}
            >
              <option value="">Default (published/active)</option>
              {(selectedPrompt?.versions ?? []).map((v) => (
                <option key={v.version} value={v.version}>
                  v{v.version}{selectedPrompt && v.version === selectedPrompt.activeVersion ? " (active)" : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Context source" hint="Where the caller's runtime context comes from for this turn.">
            <select
              className="select" value={contextSource} aria-label="Simulator context source"
              onChange={(e) => setContextSource(e.target.value as typeof contextSource)}
            >
              <option value="saved">Saved config</option>
              <option value="manual">Manual JSON</option>
              <option value="api_mock">Mock API response</option>
              <option value="none">None</option>
            </select>
          </Field>
          <Field label="Language">
            <select className="select" value={language} aria-label="Simulator language" onChange={(e) => setLanguage(e.target.value)}>
              <option value="">Bot default</option>
              <option value="en-US">English (en-US)</option>
              <option value="hi-IN">Hindi (hi-IN)</option>
            </select>
          </Field>
        </div>

        {(contextSource === "manual" || contextSource === "api_mock") && (
          <Field label={contextSource === "manual" ? "Context payload (JSON)" : "Mock User Details API response (JSON)"}>
            <textarea
              className="textarea mono" rows={4} value={contextJson}
              style={{ fontSize: 12 }}
              onChange={(e) => setContextJson(e.target.value)}
            />
          </Field>
        )}

        <div className="row gap-16 wrap">
          <span className="row gap-8">
            <Toggle checked={isFinal} onChange={setIsFinal} label="Final transcript" />
            <span className="t-sub" style={{ fontSize: 12.5 }}>Final transcript</span>
          </span>
          <span className="row gap-8">
            <Toggle checked={interrupted} onChange={setInterrupted} label="Caller interrupted the bot" />
            <span className="t-sub" style={{ fontSize: 12.5 }}>Caller interrupted the bot</span>
          </span>
        </div>

        <details>
          <summary className="t-micro" style={{ cursor: "pointer", fontWeight: 650 }}>
            Mock tool results — {"{tool_name: payload}"} replaces live HTTP for tools and workflows
          </summary>
          <textarea
            className="textarea mono mt-8" rows={4} value={mockToolsJson}
            aria-label="Mock tool results JSON"
            placeholder={'{\n  "payment_status": { "status": "paid", "amount": 4500 }\n}'}
            style={{ fontSize: 12, width: "100%" }}
            onChange={(e) => setMockToolsJson(e.target.value)}
          />
        </details>

        {messages.length > 0 && (
          <div className="col gap-8" style={{ maxHeight: 220, overflowY: "auto" }}>
            {messages.map((m, i) => (
              <div key={i} className={`transcript-bubble ${m.role === "user" ? "user" : "bot"}`}>
                <span className="transcript-text">{m.content}</span>
                <time className="transcript-bubble-time" dateTime={m.at} title={m.at}>
                  {formatChatTime(m.at)}
                </time>
              </div>
            ))}
          </div>
        )}

        <div className="row gap-8">
          <input
            className="input"
            placeholder="Caller message — runs one full runtime turn"
            value={input}
            aria-label="Runtime simulator input"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void send()}
          />
          <Button variant="primary" icon="play" busy={busy} disabled={!input.trim()} onClick={() => void send()}>
            Simulate
          </Button>
          <Button variant="ghost" icon="refresh" disabled={busy || (messages.length === 0 && !trace)} onClick={reset}>
            Reset
          </Button>
        </div>

        {trace && <SimulateTraceView trace={trace} />}
      </div>
    </details>
  );
}

function SimulateTraceView({ trace }: { trace: SimulateTrace }) {
  const intent = trace.intent;
  const entities = Object.entries(intent?.entities ?? {}).filter(([, v]) => v !== null && v !== undefined);
  const confidencePct = intent ? Math.round(intent.confidence <= 1 ? intent.confidence * 100 : intent.confidence) : 0;

  if (trace.heldForFinal) {
    return (
      <Callout tone="info" title="Held — waiting for the final transcript">
        {trace.note ?? "Partial transcripts never become turns; send with “Final transcript” on to run the pipeline."}
      </Callout>
    );
  }

  return (
    <div className="col gap-10" data-testid="simulate-trace">
      <div className="card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
        <span className="t-label">Response</span>
        <p className="t-body" style={{ whiteSpace: "pre-wrap", margin: "8px 0 0" }}>{trace.response || "—"}</p>
      </div>

      <div className="grid grid-2" style={{ gap: 10 }}>
        <TraceRow icon="git" label="Route">
          <div className="col gap-4" style={{ fontSize: 12.5 }}>
            <span className="row gap-6 wrap">
              <code>{trace.route ?? "—"}</code>
              {trace.action && <span className="chip chip-neutral">{trace.action}</span>}
            </span>
            {trace.routerDecision && (
              <span className="t-micro">
                router: {trace.routerDecision.route} · {trace.routerDecision.reason} · {Math.round(trace.routerDecision.confidence * 100)}%
              </span>
            )}
          </div>
        </TraceRow>

        <TraceRow icon="target" label="Intent">
          {intent ? (
            <div className="col gap-4" style={{ fontSize: 12.5 }}>
              <span className="row gap-6 wrap">
                <code>{intent.intent ?? "none"}</code>
                <span className={`t-num t-strong ${confidencePct < 70 ? "t-bad" : "t-good"}`}>{confidencePct}%</span>
                {intent.source && <span className="chip chip-neutral">{intent.source}</span>}
                {intent.below_threshold && <span className="chip chip-warning">below threshold</span>}
              </span>
              {trace.signal && <span className="t-micro">signal: {trace.signal}</span>}
              {entities.length > 0 && (
                <span className="row gap-4 wrap">
                  {entities.map(([k, v]) => (
                    <span key={k} className="chip chip-info">{k}={chipValue(v)}</span>
                  ))}
                </span>
              )}
            </div>
          ) : (
            <span className="t-micro">
              deterministic turn — no classification ran{trace.signal ? ` · signal: ${trace.signal}` : ""}
            </span>
          )}
        </TraceRow>

        {trace.policy && (
          <TraceRow icon="shield" label="Call policy">
            <div className="col gap-4" style={{ fontSize: 12.5 }}>
              <span className="row gap-6 wrap">
                <span className="chip chip-brand">phase: {trace.policy.phase}</span>
                {trace.policy.handoff && <span className="chip chip-warning">handoff</span>}
                {trace.policy.closeAfterReply && <span className="chip chip-neutral">close after reply</span>}
                {trace.policy.forceLlm && <span className="chip chip-neutral">force LLM</span>}
              </span>
              {trace.policy.blockers.length > 0 && (
                <span className="t-micro">blockers: {trace.policy.blockers.join(", ")}</span>
              )}
              {(trace.dispositionAfterTurn ?? trace.policy.disposition) && (
                <span className="t-micro">disposition: {trace.dispositionAfterTurn ?? trace.policy.disposition}</span>
              )}
            </div>
          </TraceRow>
        )}

        {trace.workflow && (
          <TraceRow icon="workflow" label="Workflow">
            <div className="col gap-4" style={{ fontSize: 12 }}>
              <span className="row gap-6">
                <code>{trace.workflow.name}</code>
                <span className={`chip ${trace.workflow.done ? "chip-neutral" : "chip-good"}`}>
                  {trace.workflow.done ? "finished" : trace.workflow.status}
                </span>
                {trace.workflow.offScript && <span className="chip chip-warning">off-script</span>}
              </span>
              {trace.workflow.nodeTrace.length > 0 && (
                <span className="t-micro">nodes: {trace.workflow.nodeTrace.join(" → ")}</span>
              )}
              {Object.keys(trace.workflow.slots).length > 0 && (
                <span className="t-micro">
                  collected: {Object.entries(trace.workflow.slots).map(([k, v]) => `${k}=${chipValue(v)}`).join(", ")}
                </span>
              )}
            </div>
          </TraceRow>
        )}

        {trace.voiceIdentity && (
          <TraceRow icon="mic" label="Voice identity">
            <span className="row gap-6 wrap" style={{ fontSize: 12.5 }}>
              <span className="t-strong">{trace.voiceIdentity.name || "Unnamed speaker"}</span>
              <span className="chip chip-neutral">{trace.voiceIdentity.gender}</span>
              <span className="t-micro">catalog metadata</span>
            </span>
          </TraceRow>
        )}

        {trace.runtimeContext && (
          <TraceRow icon="database" label="Runtime context">
            <div className="col gap-4" style={{ fontSize: 12 }}>
              {trace.runtimeContext.values.length === 0
                ? <span className="t-micro">no values on this call</span>
                : (
                  <span className="row gap-4 wrap">
                    {trace.runtimeContext.values.map((v) => (
                      <span key={v.key} className="chip chip-neutral">{v.key}={chipValue(v.value)}</span>
                    ))}
                  </span>
                )}
              {trace.runtimeContext.missingRequired.length > 0 && (
                <span className="t-micro" style={{ color: "var(--status-warning)" }}>
                  missing required: {trace.runtimeContext.missingRequired.join(", ")}
                </span>
              )}
              <span className="t-micro">domain policy: {trace.runtimeContext.domainPolicy}</span>
            </div>
          </TraceRow>
        )}

        {trace.tool && (
          <TraceRow icon="zap" label="Tool call">
            <div className="col gap-6" style={{ fontSize: 12.5 }}>
              <span className="row gap-6 wrap">
                <code>{chipValue(trace.tool.request.tool ?? "")}</code>
                <StatusChip status={trace.tool.ok ? "good" : "failed"} label={trace.tool.ok ? "OK" : "Failed"} />
                {trace.tool.status != null && <span className="tag t-num">HTTP {trace.tool.status}</span>}
                {trace.tool.mocked && <span className="chip chip-neutral">mocked</span>}
                {trace.tool.latencyMs != null && <span className="t-micro t-num">{trace.tool.latencyMs}ms</span>}
              </span>
              {trace.tool.error && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{trace.tool.error}</span>}
              <JsonView value={{ request: trace.tool.request, response: trace.tool.response }} />
            </div>
          </TraceRow>
        )}
      </div>

      {trace.renderedPrompt && (
        <details>
          <summary className="t-micro" style={{ cursor: "pointer", fontWeight: 650 }}>
            Rendered prompt — exactly what the LLM received ({trace.renderedPrompt.length.toLocaleString()} chars)
          </summary>
          <pre className="mt-8" style={simPreStyle}>{trace.renderedPrompt}</pre>
        </details>
      )}

      <span className="t-micro t-num row gap-8 wrap">
        {trace.language && <span>{trace.language}</span>}
        <span>{trace.latencyMs}ms</span>
        {trace.promptVersion != null
          ? <span>prompt v{trace.promptVersion}{trace.promptMode ? ` (${trace.promptMode})` : ""}{trace.promptState ? ` · ${trace.promptState}` : ""}</span>
          : <span>prompt: built-in default</span>}
        {trace.provider && <span>{providerName(trace.provider)}</span>}
        {trace.paymentVerification && <span>payment: {trace.paymentVerification}</span>}
      </span>
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
