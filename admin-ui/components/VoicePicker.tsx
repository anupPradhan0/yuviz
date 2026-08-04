"use client";

import { useState } from "react";
import { ApiError, ProviderConfig, createProvider } from "@/lib/api";
import { VOICES_BY_ENGINE, VoiceGender } from "@/lib/engineCatalog";

const ENGINE_LABELS: Record<string, string> = {
  macos: "macOS say (local)",
  kokoro: "Kokoro (local)",
};

const GENDER_BADGE: Record<VoiceGender, string> = {
  female: "♀",
  male: "♂",
  neutral: "⚬",
};

const GENDER_FILTERS: { value: VoiceGender | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "female", label: "Female" },
  { value: "male", label: "Male" },
  { value: "neutral", label: "Neutral" },
];

// Voices, not providers — this picker is deliberately decoupled from "which
// TTS provider config is this," even though under the hood selecting a
// voice still resolves to one (find-or-create by engine+voice, then set
// agent.tts_config_id — the exact same field the raw provider dropdown in
// Provider Assignments sets). ElevenLabs isn't listed here: its voices are
// account-specific voice_ids with no catalog to browse (see
// VOICES_BY_ENGINE's null entry) — those stay managed via the Providers
// settings page instead.
//
// Gender filter/badges are a picker-only convenience (see engineCatalog.ts's
// VoiceGender docstring) — never sent to any TTS engine; the voice id
// itself is still the only thing persisted (provider_configs.voice).
//
// No audio preview yet — would need a new synthesis endpoint (Config
// Service doesn't own a live TTS runtime; only Conversation Service does,
// and only over gRPC) — deferred rather than built half-working.
export function VoicePicker({
  tenantId,
  providers,
  value,
  onChange,
  onProviderCreated,
}: {
  tenantId: string;
  providers: ProviderConfig[];
  value: string | null | undefined;
  onChange: (providerId: string) => void;
  onProviderCreated: (provider: ProviderConfig) => void;
}) {
  const [creatingKey, setCreatingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [genderFilter, setGenderFilter] = useState<VoiceGender | "all">("all");

  const selected = providers.find((p) => p.id === value);

  const handlePick = async (engine: string, voice: string) => {
    setError(null);
    const existing = providers.find((p) => p.role === "tts" && p.engine === engine && p.voice === voice);
    if (existing) {
      onChange(existing.id);
      return;
    }
    setCreatingKey(`${engine}:${voice}`);
    try {
      const created = await createProvider(tenantId, { name: `${engine} — ${voice}`, role: "tts", engine, voice });
      onProviderCreated(created);
      onChange(created.id);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setCreatingKey(null);
    }
  };

  const entries = Object.entries(VOICES_BY_ENGINE).filter(
    (entry): entry is [string, NonNullable<(typeof VOICES_BY_ENGINE)[string]>] => entry[1] !== null,
  );
  const availableGenders = new Set(entries.flatMap(([, voices]) => voices.map((v) => v.gender)));

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}

      <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
        {GENDER_FILTERS.filter((f) => f.value === "all" || availableGenders.has(f.value)).map((f) => (
          <button
            key={f.value}
            type="button"
            className={`btn btn-sm ${genderFilter === f.value ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setGenderFilter(f.value)}
          >
            {f.value !== "all" && <span style={{ marginRight: 4 }}>{GENDER_BADGE[f.value]}</span>}
            {f.label}
          </button>
        ))}
      </div>

      {entries.map(([engine, voices]) => {
        const filtered = genderFilter === "all" ? voices : voices.filter((v) => v.gender === genderFilter);
        if (filtered.length === 0) return null;
        return (
          <div key={engine} style={{ marginBottom: 12 }}>
            <div style={{ fontSize: ".68rem", fontWeight: 700, color: "var(--text-3)", marginBottom: 6, textTransform: "uppercase", letterSpacing: ".03em" }}>
              {ENGINE_LABELS[engine] || engine}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {filtered.map(({ id: voiceId, label, gender }) => {
                const isSelected = selected?.engine === engine && selected?.voice === voiceId;
                const isCreating = creatingKey === `${engine}:${voiceId}`;
                return (
                  <button
                    key={voiceId}
                    type="button"
                    className={`btn btn-sm ${isSelected ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => handlePick(engine, voiceId)}
                    disabled={isCreating}
                    title={voiceId}
                  >
                    {isCreating ? "…" : (
                      <>
                        <span style={{ marginRight: 4, opacity: 0.6 }}>{GENDER_BADGE[gender]}</span>
                        {label}
                      </>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}

      <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
        ElevenLabs voices are account-specific — assign those from Settings → Speech Services instead.
        Kokoro voices not yet used on this machine download automatically on first call.
      </div>
    </div>
  );
}
