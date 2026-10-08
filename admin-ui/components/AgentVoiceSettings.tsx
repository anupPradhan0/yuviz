"use client";

// Language, voice, speech recognition and AI model for one agent — shared by the
// creation wizard and the agent page so both read the same.

import { Dispatch, SetStateAction, useEffect, useState } from "react";
import Link from "next/link";
import { AlertCircle } from "lucide-react";
import { AgentUpdate, ApiError, ProviderConfig, listProviders, updateProvider } from "@/lib/api";
import {
  LANGUAGES, OTHER, SUPPORTED_LANGUAGES, BrowsableTtsEngine, asBrowsableTtsEngine, baseLanguage, languageLabel,
  ttsLanguages,
} from "@/lib/engineCatalog";
import { LocalVoicePicker } from "@/components/LocalVoicePicker";
import { ElevenLabsVoicePicker } from "@/components/ElevenLabsVoicePicker";

type Role = "stt" | "llm" | "tts";

// The supported list as the server stores it: default first, de-duplicated. Fewer than
// two languages = single-language agent, so [] (detection off).
export function effectiveLanguages(language: string | null, supported: string[] | null | undefined): string[] {
  if (!supported?.length) return [];
  const out: string[] = [];
  for (const code of [baseLanguage(language), ...supported.map(baseLanguage)]) {
    if (code && !out.includes(code)) out.push(code);
  }
  return out.length > 1 ? out : [];
}

// PATCH fields for the multilingual settings. Entries for unsupported languages and blank
// ones are dropped; a single-language agent clears all three.
export function multilingualPayload(
  language: string | null,
  supported: string[] | null | undefined,
  ttsByLanguage: Record<string, string> | null | undefined,
  greetingByLanguage: Record<string, string> | null | undefined,
): Pick<AgentUpdate, "supported_languages" | "tts_config_by_language" | "greeting_by_language"> {
  const langs = effectiveLanguages(language, supported);
  if (langs.length === 0) return { supported_languages: null, tts_config_by_language: null, greeting_by_language: null };
  const keep = (m: Record<string, string> | null | undefined) => {
    const out: Record<string, string> = {};
    for (const l of langs) if (m?.[l]?.trim()) out[l] = m[l].trim();
    return Object.keys(out).length ? out : null;
  };
  return { supported_languages: langs, tts_config_by_language: keep(ttsByLanguage), greeting_by_language: keep(greetingByLanguage) };
}

// The Config Service's 400s for language/voice coverage all name the language field or say
// it can't be spoken; those belong next to the languages section, not just in the banner.
// The server skips English (every engine reads it); every other language needs a voice that speaks it.
function canSpeak(p: ProviderConfig, lang: string): boolean {
  return lang === "en" || ttsLanguages(p).includes(lang);
}

const KOKORO_EXAMPLE_VOICES: Record<string, string> = {
  hi: "hf_alpha or hm_omega", en: "af_heart", es: "ef_dora", fr: "ff_siwis", it: "if_sara",
  pt: "pf_dora", ja: "jf_alpha", zh: "zf_xiaobei",
};

function kokoroExample(lang: string): string {
  return KOKORO_EXAMPLE_VOICES[lang] ?? "one whose id starts with that language's letter";
}

export function isLanguageError(detail: string): boolean {
  return /language|can't be spoken/i.test(detail);
}

const VOICE_ENGINES: { key: BrowsableTtsEngine; title: string; blurb: string }[] = [
  { key: "kokoro", title: "Natural", blurb: "Free, runs on your server. Sounds human for most calls." },
  { key: "elevenlabs", title: "Premium", blurb: "ElevenLabs. The most realistic voices — needs an API key." },
  { key: "macos", title: "Basic", blurb: "Free and instant, but clearly robotic. Good for quick tests." },
];

export function AgentVoiceSettings({
  tenantId, providers, setProviders,
  languageChoice, onLanguageChoice, customLanguage, onCustomLanguage,
  supportedLanguages, onSupportedLanguages, ttsByLanguage, onTtsByLanguage,
  greetingByLanguage, onGreetingByLanguage, languagesError,
  sttId, llmId, ttsId, onAssign, onError,
}: {
  tenantId: string;
  providers: ProviderConfig[];
  setProviders: Dispatch<SetStateAction<ProviderConfig[]>>;
  languageChoice: string;
  onLanguageChoice: (v: string) => void;
  customLanguage: string;
  onCustomLanguage: (v: string) => void;
  supportedLanguages: string[];
  onSupportedLanguages: (v: string[]) => void;
  ttsByLanguage: Record<string, string>;
  onTtsByLanguage: (v: Record<string, string>) => void;
  greetingByLanguage: Record<string, string>;
  onGreetingByLanguage: (v: Record<string, string>) => void;
  // The server's 400 detail for the language fields, shown inline.
  languagesError?: string | null;
  sttId: string | null | undefined;
  llmId: string | null | undefined;
  ttsId: string | null | undefined;
  onAssign: (role: Role, id: string | null) => void;
  onError: (message: string) => void;
}) {
  // null = follow the engine of the assigned voice, so browsing never swaps providers on its own.
  const [chosenEngine, setChosenEngine] = useState<BrowsableTtsEngine | null>(null);
  const [speedDraft, setSpeedDraft] = useState<number | null>(null);

  // Voices are often created on the AI & Voice page mid-setup: re-read the list when this
  // panel opens and whenever the browser tab regains focus, so new ones show up here.
  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      listProviders(tenantId)
        .then((provs) => { if (!cancelled) setProviders(provs); })
        .catch(() => {});  // keep the list we have; the page's own load reports real failures
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => { cancelled = true; window.removeEventListener("focus", refresh); };
  }, [tenantId, setProviders]);

  const byRole = (role: Role) => providers.filter((p) => p.role === role);
  const selectedTts = providers.find((p) => p.id === ttsId) ?? null;
  const selectedLlm = providers.find((p) => p.id === llmId) ?? null;
  const selectedStt = providers.find((p) => p.id === sttId) ?? null;

  const defaultLanguage = baseLanguage(languageChoice === OTHER ? customLanguage : languageChoice);
  const languages = effectiveLanguages(defaultLanguage, supportedLanguages);
  const multilingual = languages.length > 0;
  // With no default picked, the server makes the first supported language the default.
  const startLanguage = multilingual ? languages[0] : null;

  const toggleLanguage = (code: string) => {
    if (multilingual && code === defaultLanguage) return; // the chosen default is always supported
    onSupportedLanguages(
      supportedLanguages.includes(code) ? supportedLanguages.filter((c) => c !== code) : [...supportedLanguages, code],
    );
  };

  const setLanguageEntry = (
    map: Record<string, string>, onChange: (v: Record<string, string>) => void, lang: string, value: string,
  ) => {
    const next = { ...map };
    if (value) next[lang] = value;
    else delete next[lang];
    onChange(next);
  };

  // .en Whisper models can't hear any other language; Deepgram only code-switches on nova-2/3.
  const sttLanguageWarning = !multilingual || !selectedStt
    ? null
    : selectedStt.engine === "faster_whisper" && selectedStt.model?.endsWith(".en")
      ? `${selectedStt.model} only understands English. Use a multilingual model (e.g. small) so callers can be heard in every language.`
      : selectedStt.engine === "deepgram" && selectedStt.model && !/^nova-[23]/.test(selectedStt.model)
        ? `${selectedStt.model} can't switch languages mid-call; it will listen in the default language only. Use nova-2 or nova-3.`
        : null;
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
          <div className="form-group">
            <label className="form-label">{multilingual ? "Default language" : "What language will callers speak?"}</label>
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
            <div className="form-hint">
              {multilingual
                ? "The agent greets callers in this language, then follows whichever language they speak."
                : "Used to understand callers and to speak back, in place of the language set on the speech recognition and voice providers. Picking a voice sets this for you."}
            </div>
          </div>

          <div className="form-group" style={{ marginBottom: 0 }}>
            <label className="form-label">
              Languages callers may switch to <span className="hint">optional</span>
            </label>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }} role="group" aria-label="Supported languages">
              {SUPPORTED_LANGUAGES.map((l) => {
                const isDefault = multilingual && l.value === startLanguage;
                const on = isDefault || supportedLanguages.some((c) => baseLanguage(c) === l.value);
                return (
                  <button
                    key={l.value}
                    type="button"
                    aria-pressed={on}
                    className={`btn btn-sm ${on ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => toggleLanguage(l.value)}
                    title={isDefault && l.value === defaultLanguage ? "The default language is always included" : undefined}
                  >
                    {languageLabel(l.value)}{isDefault ? " · default" : ""}
                  </button>
                );
              })}
            </div>
            <div className="form-hint">
              The language above alone sets the speech recognition and voice language. Adding languages here turns on
              language detection: the agent notices which language the caller speaks, switches to it, and replies in it.
              {!multilingual && supportedLanguages.length > 0 && " Pick at least two languages (including the default)."}
            </div>
          </div>

          {multilingual && (
            <div style={{ marginTop: 14 }}>
              {languages.map((lang) => (
                <div key={lang} style={{ borderTop: "1px solid var(--border-2)", paddingTop: 12, marginTop: 12 }}>
                  <div className="form-label" style={{ marginBottom: 8 }}>
                    {languageLabel(lang)}{lang === startLanguage && <span className="hint">default</span>}
                  </div>
                  <div className="form-row" style={{ flexWrap: "wrap" }}>
                    <div className="form-group">
                      <label className="form-label">Voice</label>
                      <select
                        className="form-select"
                        value={ttsByLanguage[lang] ?? ""}
                        onChange={(e) => setLanguageEntry(ttsByLanguage, onTtsByLanguage, lang, e.target.value)}
                      >
                        <option value="">
                          Use agent voice{selectedTts && !canSpeak(selectedTts, lang) ? ` — can't speak ${languageLabel(lang)}` : ""}
                        </option>
                        {byRole("tts").map((p) => {
                          const ok = canSpeak(p, lang);
                          return (
                            // Disabled rather than hidden, so a saved-but-wrong pick still shows as selected.
                            <option key={p.id} value={p.id} disabled={!ok && ttsByLanguage[lang] !== p.id}>
                              {p.name}{p.environment !== "prod" ? ` (${p.environment})` : ""}
                              {ok ? "" : ` — can't speak ${languageLabel(lang)}`}
                            </option>
                          );
                        })}
                      </select>
                      {(() => {
                        const voice = providers.find((p) => p.id === ttsByLanguage[lang]) ?? selectedTts;
                        if (!voice || canSpeak(voice, lang)) return null;
                        return (
                          <div className="voice-missing">
                            <AlertCircle size={13} />
                            <span>
                              {voice.name} can&apos;t speak {languageLabel(lang)}.{" "}
                              <Link href="/ai-voice">Add a {languageLabel(lang)} voice in AI &amp; Voice</Link>
                              {voice.engine === "kokoro" ? ` (for Kokoro, e.g. ${kokoroExample(lang)})` : ""}, then pick it here.
                            </span>
                          </div>
                        );
                      })()}
                    </div>
                    <div className="form-group">
                      <label className="form-label">Greeting</label>
                      <textarea
                        className="form-textarea"
                        style={{ minHeight: 56 }}
                        value={greetingByLanguage[lang] ?? ""}
                        onChange={(e) => setLanguageEntry(greetingByLanguage, onGreetingByLanguage, lang, e.target.value)}
                        placeholder={lang === startLanguage ? "Leave blank to use the opening line" : "Optional"}
                      />
                    </div>
                  </div>
                </div>
              ))}
              <div className="form-hint">
                Only the default language&apos;s greeting is spoken, when the call starts. Every language needs a voice
                that can speak it — the agent voice, or one picked here.
              </div>
            </div>
          )}

          {languagesError && (
            <div className="voice-missing">
              <AlertCircle size={13} />
              <span>{languagesError}</span>
            </div>
          )}
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
          {sttLanguageWarning && (
            <div className="voice-missing" style={{ marginTop: 0, marginBottom: 14 }}>
              <AlertCircle size={13} />
              <span>{sttLanguageWarning}</span>
            </div>
          )}
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
