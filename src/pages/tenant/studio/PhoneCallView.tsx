import { useEffect, useState } from "react";
import type { VoiceBot } from "@/types/domain";
import { Icon } from "@/components/Icon";

/* Caller-style phone screen over the SAME live voice session the test
   console drives (one VoiceClient, owned by TestingTab). No prompts,
   traces, transcript or other test/debug surfaces here — just the call,
   exactly like a real phone's in-call screen. */

export interface PhoneCallViewProps {
  bot: VoiceBot;
  /** Dial-in number from the bot's voice channel config (null = none configured / not visible to this role). */
  phoneNumber: string | null;
  numberLoading: boolean;
  /** Telephony provider from the channel config, e.g. "freeswitch". */
  providerLabel?: string;
  callActive: boolean;
  connecting: boolean;
  botSpeaking: boolean;
  /** Human-readable current conversation language ("Hindi"), when live. */
  languageLabel?: string;
  muted: boolean;
  onToggleMute: () => void;
  onStartCall: () => void;
  onEndCall: () => void;
}

export function formatCallDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export default function PhoneCallView({
  bot, phoneNumber, numberLoading, providerLabel,
  callActive, connecting, botSpeaking, languageLabel,
  muted, onToggleMute, onStartCall, onEndCall,
}: PhoneCallViewProps) {
  const [seconds, setSeconds] = useState(0);

  /* Call timer — runs while the session is live; the final duration stays
     on screen after hangup until the next call starts. */
  useEffect(() => {
    if (!callActive) return;
    setSeconds(0);
    const startedAt = Date.now();
    const id = window.setInterval(
      () => setSeconds(Math.floor((Date.now() - startedAt) / 1000)),
      500,
    );
    return () => window.clearInterval(id);
  }, [callActive]);

  return (
    <div className="phonecall-wrap">
      <div
        className={`phonecall${callActive ? " on-call" : ""}${connecting ? " connecting" : ""}${botSpeaking ? " speaking" : ""}`}
        data-testid="phone-call-view"
      >
        <div className="phonecall-carrier">
          <span className="row gap-6"><Icon name="phone" size={12} /> EchoSphere Voice</span>
          {callActive && languageLabel ? <span>{languageLabel}</span> : null}
        </div>

        <div className="phonecall-identity">
          <div className="phonecall-avatar">
            <span className="phonecall-ring" />
            <span className="phonecall-ring delayed" />
            <Icon name="bot" size={42} />
          </div>
          <div className="phonecall-name">{bot.name}</div>
          <div className="phonecall-number" data-testid="phone-call-number">
            {numberLoading ? "…" : phoneNumber ?? "No voice number assigned"}
          </div>
          {phoneNumber && providerLabel ? (
            <div className="phonecall-provider">via {providerLabel}</div>
          ) : null}
        </div>

        <div className="phonecall-status" role="status">
          {connecting ? (
            <><span className="spinner" /> Calling…</>
          ) : callActive ? (
            <>
              <span className={`phonecall-live-dot${botSpeaking ? " speaking" : ""}`} />
              <span className="t-num">{formatCallDuration(seconds)}</span>
              <span>· {botSpeaking ? "Speaking" : "Listening"}</span>
            </>
          ) : seconds > 0 ? (
            <>Call ended · <span className="t-num">{formatCallDuration(seconds)}</span></>
          ) : (
            "Ready to call"
          )}
        </div>

        <div className="phonecall-controls">
          <button
            type="button"
            className={`phonecall-btn${muted ? " is-muted" : ""}`}
            onClick={onToggleMute}
            disabled={!callActive}
            aria-pressed={muted}
            aria-label={muted ? "Unmute microphone" : "Mute microphone"}
            title={muted ? "Unmute microphone" : "Mute microphone"}
          >
            <Icon name={muted ? "mic-off" : "mic"} size={22} />
          </button>
          {callActive || connecting ? (
            <button
              type="button"
              className="phonecall-btn main hangup"
              onClick={onEndCall}
              disabled={connecting}
              aria-label="End call"
              title="End call"
            >
              <Icon name="phone" size={26} style={{ transform: "rotate(135deg)" }} />
            </button>
          ) : (
            <button
              type="button"
              className="phonecall-btn main call"
              onClick={onStartCall}
              aria-label="Start call"
              title="Start call"
            >
              <Icon name="phone" size={26} />
            </button>
          )}
          {/* keeps the main button centered against the mute button */}
          <span className="phonecall-btn spacer" aria-hidden="true" />
        </div>

        <div className="phonecall-hint">
          {callActive
            ? "Live call — speak into your microphone."
            : "Starts a live browser call through the bot's real voice pipeline."}
        </div>
      </div>
    </div>
  );
}
