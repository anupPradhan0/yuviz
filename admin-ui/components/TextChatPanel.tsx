"use client";

// Text-chat testing for a workflow. Same session the voice test opens —
// services/webcall's bridge, and behind it the identical Converse RPC — but
// with ?mode=text, which makes Conversation Service skip STT and TTS and
// answer in words (SessionOpenRequest.text_only). Everything in between is
// the code a real call runs: the graph walk, per-node prompts and tools,
// knowledge retrieval, extraction, end-call and transfer.
//
// Deliberately not a mode inside TestAgentPanel: that component is 500
// lines of AudioWorklet, VAD tuning and playback scheduling, none of which
// a chat has any use for.

import { useEffect, useRef, useState } from "react";

const WEBCALL_URL = process.env.NEXT_PUBLIC_WEBCALL_URL || "ws://localhost:8300";

type Turn = { role: "you" | "agent" | "note"; text: string };
type ChatState = "connecting" | "ready" | "thinking" | "ended" | "error";

export function TextChatPanel({
  open,
  onClose,
  tenantSlug,
  agentSlug,
  useDraft = false,
  onNodeChanged,
}: {
  open: boolean;
  onClose: () => void;
  tenantSlug: string;
  agentSlug: string;
  useDraft?: boolean;
  onNodeChanged?: (node: { id: string; name: string; type: string }) => void;
}) {
  const [state, setState] = useState<ChatState>("connecting");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);
  // The callback is captured once by ws.onmessage; a ref keeps it current
  // without tearing the socket down every time the parent re-renders.
  const nodeChangedRef = useRef(onNodeChanged);
  useEffect(() => { nodeChangedRef.current = onNodeChanged; }, [onNodeChanged]);

  useEffect(() => {
    if (!open) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState("connecting");
    setTurns([]);
    setError(null);

    const ws = new WebSocket(
      `${WEBCALL_URL}/webcall?mode=text&tenant=${encodeURIComponent(tenantSlug)}` +
        `&agent=${encodeURIComponent(agentSlug)}${useDraft ? "&draft=1" : ""}`,
    );
    wsRef.current = ws;

    ws.onopen = () => setState("ready");
    ws.onerror = () => {
      setError("Couldn't reach the test service. Is the webcall service running?");
      setState("error");
    };
    ws.onclose = () => setState((s) => (s === "error" ? s : "ended"));
    ws.onmessage = (ev) => {
      // Nothing binary is ever sent in this mode; ignore it rather than
      // trying to parse audio as JSON if a mode ever gets crossed.
      if (typeof ev.data !== "string") return;
      let msg: Record<string, string>;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      switch (msg.type) {
        case "agent_text":
          setTurns((t) => [...t, { role: "agent", text: msg.text }]);
          setState("ready");
          break;
        case "stt_result":
          // The service echoes the turn it actually processed. An empty one
          // means it decided there was nothing to answer.
          break;
        case "workflow_node":
          nodeChangedRef.current?.({
            id: msg.node_id, name: msg.node_name, type: msg.node_type,
          });
          break;
        case "transfer":
          setTurns((t) => [...t, {
            role: "note",
            text: `The agent handed the call to ${msg.destination || "a human"}${msg.reason ? ` — ${msg.reason}` : ""}.`,
          }]);
          setState("ended");
          break;
        case "end_call":
          setTurns((t) => [...t, { role: "note", text: "The agent ended the call." }]);
          setState("ended");
          break;
        case "no_response":
          setError(msg.message);
          setState("ready");
          break;
        case "error":
          setError(msg.message || "The agent hit an error.");
          setState(msg.fatal ? "error" : "ready");
          break;
      }
    };

    return () => {
      ws.onclose = null;   // unmount is not "the agent ended the call"
      ws.close();
      wsRef.current = null;
    };
  }, [open, tenantSlug, agentSlug, useDraft]);

  // Newest turn in view. Chats are read from the bottom.
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [turns, state]);

  if (!open) return null;

  const busy = state === "thinking";
  const closed = state === "ended" || state === "error";

  const send = () => {
    const text = draft.trim();
    // Guard the socket as well as the text: a message typed after the
    // agent hung up would otherwise vanish with no explanation.
    if (!text || busy || closed) return;
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      setError("The test session isn't connected any more. Start a new one.");
      return;
    }
    setError(null);
    setTurns((t) => [...t, { role: "you", text }]);
    setDraft("");
    setState("thinking");
    wsRef.current.send(JSON.stringify({ type: "text_input", text }));
  };

  return (
    <div className="wf-testpanel wf-chat">
      <div className="wf-chat-hdr">
        <span className="wf-chat-state">
          {state === "connecting" ? "Connecting…"
            : busy ? "Thinking…"
            : state === "ended" ? "Session over"
            : state === "error" ? "Disconnected"
            : useDraft ? "Running your draft" : "Running the live flow"}
        </span>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>End</button>
      </div>

      <div className="wf-chat-log" ref={logRef}>
        {turns.length === 0 && state !== "connecting" && (
          <div className="wf-chat-empty">
            Type what a caller would say. The stage the conversation is in lights up on the
            canvas as it moves.
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`wf-chat-turn wf-chat-${t.role}`}>
            {t.role !== "note" && <span className="wf-chat-who">{t.role === "you" ? "You" : "Agent"}</span>}
            <span className="wf-chat-text">{t.text}</span>
          </div>
        ))}
        {busy && <div className="wf-chat-turn wf-chat-agent wf-chat-typing">…</div>}
      </div>

      {error && <div className="wf-chat-error">{error}</div>}

      <div className="wf-chat-compose">
        <textarea
          className="form-input"
          rows={2}
          placeholder={closed ? "This session is over" : "Say something…"}
          value={draft}
          disabled={closed}
          onChange={(e) => setDraft(e.target.value)}
          // Enter sends, Shift+Enter is a newline — the convention every
          // chat box already has.
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button
          className="btn btn-primary btn-sm"
          onClick={send}
          disabled={busy || closed || !draft.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );
}
