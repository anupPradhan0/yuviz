"use client";

import { useEffect, useState } from "react";
import { ApiError, ElevenLabsVoice, ProviderConfig, createProvider, listElevenLabsVoices, updateProvider } from "@/lib/api";

// Unlike VoicePicker (macOS/Kokoro): ElevenLabs voices belong to one
// account, so there's no "browse then create per voice" — a provider_config
// with a real api_key_ref must exist first. If the tenant doesn't have one
// yet, this renders a one-time "connect" step (api_key_ref only, no voice)
// before it can show any voices — same zero-friction feel as local voices
// once that's done: pick a voice, it's saved immediately.
export function ElevenLabsVoicePicker({
  tenantId,
  provider,
  onProviderCreated,
  onVoicePicked,
}: {
  tenantId: string;
  provider: ProviderConfig | null;
  onProviderCreated: (provider: ProviderConfig) => void;
  onVoicePicked: (provider: ProviderConfig) => void;
}) {
  const [apiKeyRef, setApiKeyRef] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const [voices, setVoices] = useState<ElevenLabsVoice[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  // null = no explicit choice yet — defaults to English when the account
  // has it, matching ElevenLabs' own agent builder ("Default language is
  // English").
  const [languageFilter, setLanguageFilter] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!provider) return;
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
  }, [provider]);

  const handleConnect = async () => {
    setConnecting(true);
    setConnectError(null);
    try {
      const created = await createProvider(tenantId, {
        name: "ElevenLabs", role: "tts", engine: "elevenlabs", api_key_ref: apiKeyRef,
      });
      onProviderCreated(created);
    } catch (e) {
      setConnectError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setConnecting(false);
    }
  };

  if (!provider) {
    return (
      <div>
        {connectError && <div className="error-banner">{connectError}</div>}
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">
            ElevenLabs API Key Reference <span className="hint">e.g. env:ELEVENLABS_API_KEY — never a raw key, see Secret Manager</span>
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              className="form-input"
              style={{ fontFamily: "var(--mono)" }}
              value={apiKeyRef}
              onChange={(e) => setApiKeyRef(e.target.value)}
              placeholder="env:ELEVENLABS_API_KEY"
            />
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={handleConnect}
              disabled={connecting || !apiKeyRef.trim()}
            >
              {connecting ? "Connecting…" : "Connect"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  const handlePick = async (voiceId: string) => {
    setSaving(voiceId);
    setError(null);
    try {
      const updated = await updateProvider(provider.id, { voice: voiceId });
      onVoicePicked(updated);
      setExpanded(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(null);
    }
  };

  if (loading) return <div className="empty-state">Loading voices…</div>;
  if (error) return <div className="error-banner">{error}</div>;
  if (!voices || voices.length === 0) return <div className="empty-state">No voices on this ElevenLabs account.</div>;

  // Languages come from each voice's own labels — ElevenLabs has no fixed
  // list to draw from (unlike local engines' static VOICES_BY_ENGINE), so
  // the filter options are whatever languages this account's voices
  // actually have.
  const languages = Array.from(new Set(voices.map((v) => v.labels.language).filter((l): l is string => !!l))).sort();
  const activeLanguageFilter = languageFilter ?? (languages.includes("en") ? "en" : "all");
  const filteredVoices = activeLanguageFilter === "all" ? voices : voices.filter((v) => v.labels.language === activeLanguageFilter);
  const selectedVoice = voices.find((v) => v.voice_id === provider.voice);

  if (!expanded) {
    return (
      <button
        type="button"
        className="kb-row"
        style={{ width: "100%", textAlign: "left", cursor: "pointer", background: "none", border: "1px solid var(--border-2)", borderRadius: "var(--rs)" }}
        onClick={() => setExpanded(true)}
      >
        {selectedVoice ? (
          <>
            <span style={{ color: "var(--green)", marginRight: 8 }}>✓</span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>{selectedVoice.name}</div>
              <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>Primary voice</div>
            </div>
          </>
        ) : (
          <div style={{ flex: 1, color: "var(--text-3)" }}>Select a voice…</div>
        )}
        <span style={{ color: "var(--text-3)" }}>▾</span>
      </button>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ fontSize: ".78rem", fontWeight: 500 }}>
          {selectedVoice ? `Selected: ${selectedVoice.name}` : "Select a voice"}
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setExpanded(false)}>
          Close
        </button>
      </div>
      {languages.length > 1 && (
        <div style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
          <button
            type="button"
            className={`btn btn-sm ${activeLanguageFilter === "all" ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setLanguageFilter("all")}
          >
            All Languages
          </button>
          {languages.map((lang) => (
            <button
              key={lang}
              type="button"
              className={`btn btn-sm ${activeLanguageFilter === lang ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setLanguageFilter(lang)}
            >
              {lang === "en" ? "English (Default)" : lang}
            </button>
          ))}
        </div>
      )}
      {filteredVoices.length === 0 && <div className="empty-state">No voices match this language.</div>}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {filteredVoices.map((v) => {
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
                  {v.labels.language ? ` · ${v.labels.language}` : ""}
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
