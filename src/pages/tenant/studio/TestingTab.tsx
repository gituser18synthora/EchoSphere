import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { TraceStep, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listScenarios, simulateAction } from "@/services/api";
import { Button, CardSkeleton, ErrorState, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";

/* Tiny scripted engine for the simulator */
function botReply(input: string, turn: number): TraceStep {
  const lower = input.toLowerCase();
  const base = { turn, speaker: "bot" as const, latencyMs: Math.round(380 + Math.random() * 400), costUsd: 0.004 + Math.random() * 0.004, promptVersion: undefined as string | undefined };
  if (/book|appoint|see (a )?doctor|schedule/.test(lower)) {
    return { ...base, text: "Sure — which clinic works best for you, and do you have a preferred day?", intent: "book_appointment", confidence: 0.93, apiCalls: [{ name: "EHR Slot Availability", ms: 356, ok: true }] };
  }
  if (/insurance|coverage|aetna|blue cross/.test(lower)) {
    return { ...base, text: "We accept most major plans including BlueCross and UnitedHealthcare. For Medicare Advantage, coverage varies by clinic — would you like me to check a specific plan?", intent: "insurance_question", confidence: 0.64, chunksUsed: ["Insurance Providers Page §1 (stale)", "Top 60 Patient FAQs §12"] };
  }
  if (/human|person|front desk|operator|someone/.test(lower)) {
    return { ...base, text: "No problem — I'll connect you with the front desk now. Please stay on the line.", intent: "talk_to_human", confidence: 0.96, promptVersion: "escalation v5" };
  }
  if (/hour|open|close/.test(lower)) {
    return { ...base, text: "The Oakwood clinic is open 8 AM to 6 PM on weekdays and 9 AM to 1 PM on Saturdays.", intent: "clinic_hours", confidence: 0.9, chunksUsed: ["Clinic Locations & Hours §2"] };
  }
  return { ...base, text: "Sorry, I didn’t quite catch that. You can say things like “book an appointment”, “reschedule”, or “talk to the front desk”.", intent: "fallback", confidence: 0.41, promptVersion: "fallback v2" };
}

export default function TestingTab({ bot }: { bot: VoiceBot }) {
  const scenariosQ = useAsync(() => listScenarios(bot.id), [bot.id]);
  const navigate = useNavigate();
  const { toast } = useApp();
  const [steps, setSteps] = useState<TraceStep[]>([
    { turn: 1, speaker: "bot", text: "Hi, thanks for calling Meridian Health. I can help you book, change or cancel an appointment. How can I help today?", promptVersion: "greeting v4", latencyMs: 480, costUsd: 0.004 },
  ]);
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null);
  const [runningSuite, setRunningSuite] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  const send = () => {
    const text = input.trim();
    if (!text || thinking) return;
    const userStep: TraceStep = { turn: steps.length + 1, speaker: "user", text };
    setSteps((s) => [...s, userStep]);
    setInput("");
    setThinking(true);
    setTimeout(() => {
      setSteps((s) => {
        const reply = botReply(text, s.length + 1);
        setSelectedTurn(reply.turn);
        return [...s, reply];
      });
      setThinking(false);
      setTimeout(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);
    }, 700);
    setTimeout(() => listRef.current?.scrollTo({ top: 99999, behavior: "smooth" }), 60);
  };

  const selected = steps.find((s) => s.turn === selectedTurn && s.speaker === "bot") ?? [...steps].reverse().find((s) => s.speaker === "bot");
  const failing = (scenariosQ.data ?? []).filter((s) => s.lastRun && !s.lastRun.pass);

  const runSuite = async () => {
    setRunningSuite(true);
    await simulateAction("suite");
    setRunningSuite(false);
    toast("Regression suite finished: 6 passed, 2 failed — details below");
    scenariosQ.reload();
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
              <Button size="sm" variant="ghost" icon="refresh" onClick={() => { setSteps(steps.slice(0, 1)); setSelectedTurn(null); }}>Reset</Button>
              <Button size="sm" variant="ghost" icon="mic" disabled title="Voice simulation requires audio backend (TODO_BACKEND #2)">Voice mode</Button>
            </div>
          </div>
          <div ref={listRef} className="col grow" style={{ padding: 16, gap: 10, overflowY: "auto" }}>
            {steps.map((s) => (
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
