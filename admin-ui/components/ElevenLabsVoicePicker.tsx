"use client";

import { useEffect, useState } from "react";
import { ApiError, ElevenLabsVoice, ProviderConfig, listElevenLabsVoices, updateProvider } from "@/lib/api";

// Unlike VoicePicker (macOS/Kokoro): ElevenLabs voices belong to one
// account, so there's no "browse then create" — the provider_config with
// its api_key_ref must already exist, and picking a voice PATCHes that
// same config's `voice` field rather than resolving/creating a new one.
export function ElevenLabsVoicePicker({
  provider,
  onUpdated,
}: {
  provider: ProviderConfig;
  onUpdated: (provider: ProviderConfig) => void;
}) {
  const [voices, setVoices] = useState<ElevenLabsVoice[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    listElevenLabsVoices(provider.id)
      .then((v) => {
        if (!ignore) setVoices(v);
      })
      .catch((e) => {
        if (!ignore) setError(e instanceof ApiError ? e.detail : String(e));
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [provider.id]);

  const handlePick = async (voiceId: string) => {
    setSaving(voiceId);
    setError(null);
    try {
      const updated = await updateProvider(provider.id, { voice: voiceId });
      onUpdated(updated);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(null);
    }
  };

  if (loading) return <div className="empty-state">Loading voices…</div>;
  if (error) return <div className="error-banner">{error}</div>;
  if (!voices || voices.length === 0) return <div className="empty-state">No voices on this ElevenLabs account.</div>;

  return (
    <div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {voices.map((v) => {
          const isSelected = provider.voice === v.voice_id;
          const isSaving = saving === v.voice_id;
          return (
            <div key={v.voice_id} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500 }}>{v.name}</div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                  {v.category}
                  {v.labels.gender ? ` · ${v.labels.gender}` : ""}
                  {v.labels.accent ? ` · ${v.labels.accent}` : ""}
                </div>
              </div>
              {v.preview_url && <audio controls src={v.preview_url} style={{ height: 30, maxWidth: 200 }} />}
              <button
                type="button"
                className={`btn btn-sm ${isSelected ? "btn-primary" : "btn-ghost"}`}
                onClick={() => handlePick(v.voice_id)}
                disabled={isSaving}
              >
                {isSaving ? "…" : isSelected ? "Selected" : "Select"}
              </button>
            </div>
          );
        })}
      </div>
      <div style={{ fontSize: ".7rem", color: "var(--text-3)", marginTop: 10 }}>
        Voices come from this ElevenLabs account directly — add or remove voices at elevenlabs.io, then reopen this panel to refresh.
      </div>
    </div>
  );
}
