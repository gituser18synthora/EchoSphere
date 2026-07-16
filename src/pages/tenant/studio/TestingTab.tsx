import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Intent, TraceStep, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listIntents, listPrompts, listScenarios, runSuite as runSuiteApi } from "@/services/api";
import { Button, CardSkeleton, ErrorState, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";

/* Local simulator: deterministic intent matching against the bot's REAL
   intents (token overlap with their samples). Replies are clearly labelled
   as simulator output — live testing needs the runtime engine. */
const tokens = (s: string) => new Set(s.toLowerCase().split(/[^a-z0-9']+/).filter((w) => w.length > 2));

function matchIntent(input: string, intents: Intent[]): { intent?: string; confidence: number; route?: string } {
  const inTok = tokens(input);
  if (inTok.size === 0) return { confidence: 0.3 };
  let best: { intent?: string; overlap: number; route?: string } = { overlap: 0 };
  for (const it of intents) {
    for (const sample of [...it.samples, it.name.replace(/_/g, " ")]) {
      const sTok = tokens(sample);
      if (sTok.size === 0) continue;
      let inter = 0;
      for (const t of inTok) if (sTok.has(t)) inter++;
      const overlap = inter / new Set([...inTok, ...sTok]).size;
      if (overlap > best.overlap) best = { intent: it.name, overlap, route: it.route };
    }
  }
  if (best.overlap < 0.15 || !best.intent) return { confidence: Math.round((0.3 + best.overlap) * 100) / 100 };
  return { intent: best.intent, route: best.route, confidence: Math.min(0.97, Math.round((0.55 + best.overlap * 0.6) * 100) / 100) };
}

function botReply(input: string, turn: number, intents: Intent[]): TraceStep {
  const m = matchIntent(input, intents);
  const base = { turn, speaker: "bot" as const, latencyMs: 400, costUsd: 0.004 };
  if (m.intent) {
    return {
      ...base,
      text: `Simulator: matched intent “${m.intent.replace(/_/g, " ")}”${m.route ? ` → routes to ${m.route}` : ""}. Connect the runtime engine for live responses.`,
      intent: m.intent,
      confidence: m.confidence,
    };
  }
  return {
    ...base,
    text: "Simulator: no intent matched with enough confidence — this would trigger the fallback prompt.",
    intent: "fallback",
    confidence: m.confidence,
  };
}

export default function TestingTab({ bot }: { bot: VoiceBot }) {
  const scenariosQ = useAsync(() => listScenarios(bot.id), [bot.id]);
  const intentsQ = useAsync(() => listIntents(bot.id), [bot.id]);
  const promptsQ = useAsync(() => listPrompts(bot.id), [bot.id]);
  const navigate = useNavigate();
  const { toast } = useApp();
  const greetingPrompt = promptsQ.data?.find((p) => p.type === "greeting");
  const greetingText =
    greetingPrompt?.versions.find((v) => v.version === greetingPrompt.activeVersion)?.variants[0]?.content
    ?? `Simulator session for ${bot.name} — type a caller message to test intent detection.`;
  const [steps, setSteps] = useState<TraceStep[]>([]);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null);
  const [runningSuite, setRunningSuite] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  const send = () => {
    const text = input.trim();
    if (!text || thinking) return;
    const userStep: TraceStep = { turn: steps.length + 2, speaker: "user", text };
    setSteps((s) => [...s, userStep]);
    setInput("");
    setThinking(true);
    setTimeout(() => {
      setSteps((s) => {
        const reply = botReply(text, s.length + 2, intentsQ.data ?? []);
        setSelectedTurn(reply.turn);
        return [...s, reply];
      });
      setThinking(false);
      setTimeout(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);
    }, 700);
    setTimeout(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);
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
              <Button size="sm" variant="ghost" icon="refresh" onClick={() => { setSteps([]); setSelectedTurn(null); }}>Reset</Button>
              <Button size="sm" variant="ghost" icon="mic" disabled title="Voice simulation requires audio backend (TODO_BACKEND #2)">Voice mode</Button>
            </div>
          </div>
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
                  <span className="t-num t-strong">{selected.latencyMs}ms</span>
                </TraceRow>
                {flags.tenantCostVisibility && (
                  <TraceRow icon="dollar" label="Turn cost">
                    <span className="t-num t-strong">${selected.costUsd?.toFixed(4)}</span>
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
