"use client";

// Language, voice, speech recognition and AI model for one agent — shared by the
// creation wizard and the agent page so both read the same.

import { Dispatch, SetStateAction, useState } from "react";
import Link from "next/link";
import { AlertCircle, Check } from "lucide-react";
import { ApiError, ProviderConfig, createProvider, updateProvider } from "@/lib/api";
import { BUILTIN_TTS_ENGINE, LANGUAGES, OTHER } from "@/lib/engineCatalog";
import { LocalVoicePicker } from "@/components/LocalVoicePicker";
import { ElevenLabsVoicePicker } from "@/components/ElevenLabsVoicePicker";

type Role = "stt" | "llm" | "tts";

// `ready: false` = no voice-service integration yet, shown so users know it's coming.
const VOICE_SERVICES: { engine: string; name: string; blurb: string; ready: boolean }[] = [
  { engine: "elevenlabs", name: "ElevenLabs", blurb: "The most lifelike voices, many languages.", ready: true },
  { engine: "cartesia", name: "Cartesia", blurb: "Very fast, natural voices.", ready: true },
  { engine: "sarvam", name: "Sarvam", blurb: "Made for Indian languages.", ready: false },
  { engine: "smallest", name: "Smallest AI", blurb: "Low-cost, quick voices.", ready: false },
];

const SERVICE_NAMES: Record<string, string> = {
  elevenlabs: "ElevenLabs", cartesia: "Cartesia", deepgram: "Deepgram", macos: "Basic voice",
};

export function AgentVoiceSettings({
  tenantId, providers, setProviders,
  languageChoice, onLanguageChoice, customLanguage, onCustomLanguage,
  sttId, llmId, ttsId, onAssign, onError, part,
}: {
  /** Omitted = everything; the agent editor splits voice and engines across two sections. */
  part?: "voice" | "engines";
  tenantId: string;
  providers: ProviderConfig[];
  setProviders: Dispatch<SetStateAction<ProviderConfig[]>>;
  languageChoice: string;
  onLanguageChoice: (v: string) => void;
  customLanguage: string;
  onCustomLanguage: (v: string) => void;
  sttId: string | null | undefined;
  llmId: string | null | undefined;
  ttsId: string | null | undefined;
  onAssign: (role: Role, id: string | null) => void;
  onError: (message: string) => void;
}) {
  // null = follow the assigned voice: built-in unless the agent already uses another service.
  const [chosenMode, setChosenMode] = useState<"builtin" | "own" | null>(null);
  const [showConnect, setShowConnect] = useState(false);
  const [service, setService] = useState("elevenlabs");
  const [apiKey, setApiKey] = useState("");
  const [voiceId, setVoiceId] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [speedDraft, setSpeedDraft] = useState<number | null>(null);

  const byRole = (role: Role) => providers.filter((p) => p.role === role);
  const selectedTts = providers.find((p) => p.id === ttsId) ?? null;
  const selectedLlm = providers.find((p) => p.id === llmId) ?? null;
  const connectedVoices = byRole("tts").filter((p) => p.engine !== BUILTIN_TTS_ENGINE);
  const voiceMode = chosenMode ?? (selectedTts && selectedTts.engine !== BUILTIN_TTS_ENGINE ? "own" : "builtin");
  const connectOpen = showConnect || connectedVoices.length === 0;

  const replaceProvider = (updated: ProviderConfig) =>
    setProviders((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));

  const patchExtra = async (p: ProviderConfig, patch: Record<string, unknown>) => {
    try {
      replaceProvider(await updateProvider(p.id, { extra: { ...(p.extra || {}), ...patch } }));
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const applyDetectedLanguage = (l: string) => {
    if (LANGUAGES.some((x) => x.value === l)) {
      onLanguageChoice(l);
    } else {
      onLanguageChoice(OTHER);
      onCustomLanguage(l);
    }
  };

  const assignVoice = (id: string) => onAssign("tts", id);

  const handleConnect = async () => {
    const svc = VOICE_SERVICES.find((s) => s.engine === service)!;
    setConnecting(true);
    setConnectError(null);
    try {
      const created = await createProvider(tenantId, {
        name: svc.name, role: "tts", engine: svc.engine, api_key: apiKey.trim(),
        ...(svc.engine === "cartesia" ? { voice: voiceId.trim() } : {}),
      });
      setProviders((prev) => [...prev, created]);
      assignVoice(created.id);
      setApiKey("");
      setVoiceId("");
      setShowConnect(false);
    } catch (e) {
      setConnectError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setConnecting(false);
    }
  };

  const savedSpeed = Number(selectedTts?.extra?.speed ?? 1.0);
  const speed = speedDraft ?? savedSpeed;
  const commitSpeed = (v: number) => {
    setSpeedDraft(null);
    if (selectedTts && v !== savedSpeed) patchExtra(selectedTts, { speed: v });
  };

  // Must match _is_thinking_capable() in services/conversation/ai_provider_manager.py.
  const thinkingCapable = selectedLlm?.engine === "ollama" && !!selectedLlm.model?.startsWith("gemma4");

  const providerSelect = (role: "stt" | "llm", value: string | null | undefined, label: string, hint: string) => {
    const options = byRole(role);
    return (
      <div className="form-group">
        <label className="form-label">{label}</label>
        <div className="form-hint" style={{ marginTop: 0, marginBottom: 6 }}>{hint}</div>
        {options.length === 0 ? (
          <div className="voice-missing">
            <AlertCircle size={13} />
            <span>None set up yet. <Link href="/ai-voice">Add one in AI &amp; Voice</Link>.</span>
          </div>
        ) : (
          <>
            <select className="form-select" value={value || ""} onChange={(e) => onAssign(role, e.target.value || null)}>
              <option value="">Choose…</option>
              {options.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}{p.environment !== "prod" ? ` (${p.environment})` : ""}
                </option>
              ))}
            </select>
            {!value && (
              <div className="voice-missing">
                <AlertCircle size={13} />
                <span>Required — the agent can&apos;t take calls without it.</span>
              </div>
            )}
          </>
        )}
      </div>
    );
  };

  return (
    <>
      {part !== "engines" && <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-hdr">
          <div className="card-title">Language</div>
        </div>
        <div className="card-body">
          <div className="form-group" style={{ marginBottom: 0 }}>
            <label className="form-label">What language will callers speak?</label>
            <select className="form-select" value={languageChoice} onChange={(e) => onLanguageChoice(e.target.value)}>
              <option value="">Automatic — match the voice</option>
              {LANGUAGES.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
              <option value={OTHER}>Other…</option>
            </select>
            {languageChoice === OTHER && (
              <input
                className="form-input"
                style={{ marginTop: 6, fontFamily: "var(--mono)" }}
                value={customLanguage}
                onChange={(e) => onCustomLanguage(e.target.value)}
                placeholder="Short language code, e.g. nl for Dutch"
              />
            )}
            <div className="form-hint">Used to understand callers and to speak back. Picking a voice sets this for you.</div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-hdr">
          <div className="card-title">Voice</div>
          <div className="card-sub">how your agent sounds on the phone</div>
        </div>
        <div className="card-body">
          <div className="voice-engine-row" role="radiogroup" aria-label="Where the voice comes from">
            <button
              type="button"
              role="radio"
              aria-checked={voiceMode === "builtin"}
              className={`voice-engine-card${voiceMode === "builtin" ? " on" : ""}`}
              onClick={() => setChosenMode("builtin")}
            >
              <div className="agent-template-title">Built-in voice <span className="voice-default-tag">Default</span></div>
              <div className="agent-template-blurb">Included with your plan. Works right away, no setup needed.</div>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={voiceMode === "own"}
              className={`voice-engine-card${voiceMode === "own" ? " on" : ""}`}
              onClick={() => setChosenMode("own")}
            >
              <div className="agent-template-title">Your own voice service</div>
              <div className="agent-template-blurb">Use your ElevenLabs, Cartesia or other account.</div>
            </button>
          </div>

          {voiceMode === "builtin" ? (
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Choose a voice</label>
              <LocalVoicePicker
                engine={BUILTIN_TTS_ENGINE}
                tenantId={tenantId}
                providers={providers}
                value={ttsId}
                onChange={assignVoice}
                onProviderCreated={(p) => setProviders((prev) => [...prev, p])}
                onLanguageDetected={applyDetectedLanguage}
              />
            </div>
          ) : (
            <>
              {connectedVoices.length > 0 && (
                <div className="form-group">
                  <label className="form-label">Your voice services</label>
                  <div className="voice-list">
                    {connectedVoices.map((p) => (
                      <button
                        key={p.id}
                        type="button"
                        className={`voice-list-row${p.id === ttsId ? " on" : ""}`}
                        onClick={() => assignVoice(p.id)}
                      >
                        <span className="voice-list-check">{p.id === ttsId && <Check size={13} />}</span>
                        <span className="voice-list-name">{p.name}</span>
                        <span className="voice-list-meta">{SERVICE_NAMES[p.engine] ?? p.engine}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {selectedTts?.engine === "elevenlabs" && (
                <div className="form-group">
                  <label className="form-label">Which ElevenLabs voice?</label>
                  <ElevenLabsVoicePicker
                    tenantId={tenantId}
                    provider={selectedTts}
                    onProviderCreated={(p) => setProviders((prev) => [...prev, p])}
                    onVoicePicked={(updated) => {
                      replaceProvider(updated);
                      assignVoice(updated.id);
                    }}
                    onLanguageDetected={applyDetectedLanguage}
                  />
                </div>
              )}

              {!connectOpen ? (
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowConnect(true)}>
                  + Connect another service
                </button>
              ) : (
              <>
              <div className="form-group">
                <label className="form-label">Which service do you use?</label>
                <div className="voice-service-row" role="radiogroup" aria-label="Voice service">
                  {VOICE_SERVICES.map((s) => (
                    <button
                      key={s.engine}
                      type="button"
                      role="radio"
                      aria-checked={service === s.engine}
                      disabled={!s.ready}
                      className={`voice-service${service === s.engine ? " on" : ""}`}
                      onClick={() => setService(s.engine)}
                    >
                      <b>{s.name}</b>
                      <span>{s.ready ? s.blurb : "Coming soon"}</span>
                    </button>
                  ))}
                </div>
              </div>
              {connectError && <div className="error-banner">{connectError}</div>}
              <div className="form-group">
                <label className="form-label">Your {VOICE_SERVICES.find((s) => s.engine === service)?.name} API key</label>
                <input
                  className="form-input"
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="Paste your API key"
                />
                <div className="form-hint">
                  Find it in your {service === "cartesia" ? "Cartesia" : "ElevenLabs"} account settings. We store it encrypted.
                </div>
              </div>
              {service === "cartesia" && (
                <div className="form-group">
                  <label className="form-label">Voice ID</label>
                  <input
                    className="form-input"
                    value={voiceId}
                    onChange={(e) => setVoiceId(e.target.value)}
                    placeholder="Paste the voice ID"
                  />
                  <div className="form-hint">Open the voice you like in Cartesia and copy its ID.</div>
                </div>
              )}
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={handleConnect}
                disabled={connecting || !apiKey.trim() || (service === "cartesia" && !voiceId.trim())}
              >
                {connecting ? "Connecting…" : service === "elevenlabs" ? "Connect and choose a voice" : "Connect and use this voice"}
              </button>
              {connectedVoices.length > 0 && (
                <button type="button" className="btn btn-ghost btn-sm" style={{ marginLeft: 6 }} onClick={() => setShowConnect(false)}>
                  Cancel
                </button>
              )}
              </>
              )}
            </>
          )}

          <div className="form-group" style={{ marginTop: 16, marginBottom: 0 }}>
            <label className="form-label">Speaking speed</label>
            <div className="voice-speed">
              <span>Slower</span>
              <input
                type="range"
                min={0.7}
                max={1.2}
                step={0.05}
                value={speed}
                disabled={!selectedTts}
                aria-label="Speaking speed"
                onChange={(e) => setSpeedDraft(Number(e.target.value))}
                onMouseUp={(e) => commitSpeed(Number(e.currentTarget.value))}
                onTouchEnd={(e) => commitSpeed(Number(e.currentTarget.value))}
                onKeyUp={(e) => commitSpeed(Number(e.currentTarget.value))}
              />
              <span>Faster</span>
              <span className="voice-speed-value">{speed === 1 ? "Normal" : `${speed.toFixed(2)}×`}</span>
            </div>
            <div className="form-hint">
              {selectedTts
                ? "Saved right away, and shared by every agent that uses this voice."
                : "Pick a voice first."}
            </div>
          </div>
        </div>
      </div>
      </>}

      {part !== "voice" && <div className="card">
        <div className="card-hdr">
          <div className="card-title">Listening &amp; thinking</div>
          <div className="card-sub">both are needed to take calls</div>
        </div>
        <div className="card-body">
          <div className="form-row" style={{ flexWrap: "wrap" }}>
            {providerSelect("stt", sttId, "Speech recognition", "Turns what the caller says into text.")}
            {providerSelect("llm", llmId, "AI model", "Decides what to say and when to use tools.")}
          </div>
          {thinkingCapable && selectedLlm && (
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                Think before answering
                <span className="hint">smarter replies, but adds 5–8 seconds per turn</span>
              </label>
              <label className="toggle-switch">
                <input
                  type="checkbox"
                  checked={Boolean(selectedLlm.extra?.think ?? false)}
                  onChange={(e) => patchExtra(selectedLlm, { think: e.target.checked })}
                />
                <span className="toggle-slider" />
              </label>
            </div>
          )}
        </div>
      </div>}
    </>
  );
}
