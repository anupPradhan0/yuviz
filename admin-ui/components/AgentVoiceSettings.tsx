"use client";

// Language, voice, speech recognition and AI model for one agent — shared by the
// creation wizard and the agent page so both read the same.

import { Dispatch, SetStateAction, useState } from "react";
import Link from "next/link";
import { AlertCircle } from "lucide-react";
import { ApiError, ProviderConfig, updateProvider } from "@/lib/api";
import { LANGUAGES, OTHER, BrowsableTtsEngine, asBrowsableTtsEngine } from "@/lib/engineCatalog";
import { LocalVoicePicker } from "@/components/LocalVoicePicker";
import { ElevenLabsVoicePicker } from "@/components/ElevenLabsVoicePicker";

type Role = "stt" | "llm" | "tts";

const VOICE_ENGINES: { key: BrowsableTtsEngine; title: string; blurb: string }[] = [
  { key: "kokoro", title: "Natural", blurb: "Free, runs on your server. Sounds human for most calls." },
  { key: "elevenlabs", title: "Premium", blurb: "ElevenLabs. The most realistic voices — needs an API key." },
  { key: "macos", title: "Basic", blurb: "Free and instant, but clearly robotic. Good for quick tests." },
];

export function AgentVoiceSettings({
  tenantId, providers, setProviders,
  languageChoice, onLanguageChoice, customLanguage, onCustomLanguage,
  sttId, llmId, ttsId, onAssign, onError,
}: {
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
  // null = follow the engine of the assigned voice, so browsing never swaps providers on its own.
  const [chosenEngine, setChosenEngine] = useState<BrowsableTtsEngine | null>(null);
  const [speedDraft, setSpeedDraft] = useState<number | null>(null);

  const byRole = (role: Role) => providers.filter((p) => p.role === role);
  const selectedTts = providers.find((p) => p.id === ttsId) ?? null;
  const selectedLlm = providers.find((p) => p.id === llmId) ?? null;
  const engine = chosenEngine ?? asBrowsableTtsEngine(selectedTts?.engine);

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

  const assignVoice = (id: string) => {
    onAssign("tts", id);
    setChosenEngine(null);
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
                placeholder="Language code, e.g. nl-BE"
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
          <div className="voice-engine-row" role="radiogroup" aria-label="Voice quality">
            {VOICE_ENGINES.map((v) => (
              <button
                key={v.key}
                type="button"
                role="radio"
                aria-checked={engine === v.key}
                className={`voice-engine-card${engine === v.key ? " on" : ""}`}
                onClick={() => setChosenEngine(v.key)}
              >
                <div className="agent-template-title">{v.title}</div>
                <div className="agent-template-blurb">{v.blurb}</div>
              </button>
            ))}
          </div>

          {!engine ? (
            <div className="form-hint">Choose a voice type above to browse and preview its voices.</div>
          ) : engine === "elevenlabs" ? (
            <ElevenLabsVoicePicker
              tenantId={tenantId}
              // Prefer the agent's own provider; a tenant may have several ElevenLabs accounts.
              provider={(selectedTts?.engine === "elevenlabs" ? selectedTts : undefined)
                ?? providers.find((p) => p.role === "tts" && p.engine === "elevenlabs")
                ?? null}
              isCurrentAssignment={selectedTts?.engine === "elevenlabs"}
              onProviderCreated={(p) => {
                setProviders((prev) => [...prev, p]);
                assignVoice(p.id);
              }}
              onVoicePicked={(updated) => {
                replaceProvider(updated);
                assignVoice(updated.id);
              }}
              onLanguageDetected={applyDetectedLanguage}
            />
          ) : (
            <LocalVoicePicker
              engine={engine}
              tenantId={tenantId}
              providers={providers}
              value={ttsId}
              onChange={assignVoice}
              onProviderCreated={(p) => setProviders((prev) => [...prev, p])}
              onLanguageDetected={applyDetectedLanguage}
            />
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

      <div className="card">
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
      </div>
    </>
  );
}
