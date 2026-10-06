"use client";

// In-browser test call beside the agent editor (engine: lib/useWebCall).

import { useEffect, useRef } from "react";
import { Mic, MicOff } from "lucide-react";
import { useWebCall } from "@/lib/useWebCall";

const STATUS_TEXT: Record<string, string> = {
  connecting: "Connecting…",
  ready: "Listening — just start talking.",
  talking: "Hearing you…",
  thinking: "Thinking…",
  speaking: "Speaking — talk over it to interrupt.",
  ended: "Call ended.",
  error: "Something went wrong.",
};

export function AgentTestPanel({ tenantSlug, agentSlug, savePending }: {
  tenantSlug: string;
  agentSlug: string;
  /** A test must run the latest edits, so starting waits for autosave. */
  savePending: boolean;
}) {
  const call = useWebCall(tenantSlug, agentSlug);
  const logRef = useRef<HTMLDivElement | null>(null);
  const live = call.state !== "idle" && call.state !== "ended" && call.state !== "error";
  const listening = ["ready", "talking", "thinking", "speaking"].includes(call.state);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [call.transcript.length]);

  return (
    <div className="card ed-test">
      <div className="ed-test-hdr">
        <b>Test your agent</b>
        {live && <span className="badge green">On a call</span>}
      </div>

      <div className="ed-test-log" ref={logRef}>
        {call.transcript.length === 0 ? (
          <div className="ed-test-empty">
            Talk to your agent from this browser. Changes are saved before every test.
          </div>
        ) : (
          call.transcript.map((t, i) => (
            <div key={i} className={`ed-bubble${t.role === "user" ? " me" : ""}`}>{t.text}</div>
          ))
        )}
      </div>

      {STATUS_TEXT[call.state] && <div className="ed-test-status">{STATUS_TEXT[call.state]}</div>}
      {listening && (
        <div className="test-meter" aria-hidden="true">
          <div
            className="test-meter-fill"
            style={{
              width: `${call.muted ? 0 : call.micLevelPct}%`,
              background: call.state === "talking" ? "var(--red)" : "var(--green)",
            }}
          />
        </div>
      )}
      {call.errorMsg && <div className="error-banner">{call.errorMsg}</div>}

      {!live ? (
        <button className="btn btn-primary btn-sm ed-test-btn" onClick={call.start} disabled={savePending}>
          {savePending ? "Saving your changes…" : "Start test call"}
        </button>
      ) : (
        <div className="ed-test-actions">
          <button
            className={`btn btn-sm ${call.muted ? "btn-primary" : "btn-ghost"}`}
            onClick={() => call.setMuted(!call.muted)}
            aria-pressed={call.muted}
          >
            {call.muted ? <><MicOff size={13} /> Unmute</> : <><Mic size={13} /> Mute</>}
          </button>
          <button className="btn btn-danger btn-sm" onClick={call.hangUp}>End call</button>
        </div>
      )}
      <div className="ed-test-hint">Use headphones, so the agent doesn&apos;t hear itself.</div>
    </div>
  );
}
