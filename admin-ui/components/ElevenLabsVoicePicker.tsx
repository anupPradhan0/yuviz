"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, ElevenLabsVoice, ProviderConfig, createProvider, listElevenLabsVoices, updateProvider } from "@/lib/api";
import { ELEVENLABS_LANGUAGES } from "@/lib/engineCatalog";
import { SecretRefInput } from "./SecretRefInput";

// Module-level, not component state — survives the component unmounting
// (e.g. navigating away from Behaviour and back), so re-opening the Voice
// card doesn't re-hit the real ElevenLabs API every time. Resets on a full
// page reload, and explicitly on the Refresh action below. Voice lists
// change rarely enough that this is a reasonable tradeoff over either a
// TTL or no caching at all.
const voicesCache = new Map<string, ElevenLabsVoice[]>();

// verified_languages is the actual validated field (real ISO 639-1 codes
// ElevenLabs confirmed this voice speaks); labels.language is arbitrary,
// unvalidated free text an account owner typed in — prefer the former,
// fall back to the latter only when a voice has no verified languages at
// all (e.g. never run through ElevenLabs' verification).
function voicePrimaryLanguage(v: ElevenLabsVoice): string | null {
  return v.verified_languages[0]?.language ?? v.labels.language ?? null;
}

// Unlike LocalVoicePicker (macOS/Kokoro): ElevenLabs voices belong to one
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
  onLanguageDetected,
  disabled = false,
  isCurrentAssignment = true,
}: {
  tenantId: string;
  provider: ProviderConfig | null;
  onProviderCreated: (provider: ProviderConfig) => void;
  onVoicePicked: (provider: ProviderConfig) => void;
  onLanguageDetected: (language: string) => void;
  // Locks the whole picker (can't connect, can't expand/reselect) — see
  // LocalVoicePicker's disabled prop for why this exists.
  disabled?: boolean;
  // False when `provider` is a fallback ("any ElevenLabs provider on the
  // tenant") shown because the agent isn't actually assigned to an
  // ElevenLabs provider yet — see the Voice card's engine-chooser callers.
  // `provider.voice` can be non-empty in that case purely from unrelated
  // past use (another agent, earlier testing), which is real data but NOT
  // this agent's current voice — confirmed live 2026-08-21: a fallback
  // provider showing a stale "✓ Primary voice" checkmark was mistaken for
  // an actual (and wrong) agent assignment. Default true so a caller that
  // always passes the genuinely-assigned provider (or none) doesn't need
  // to think about this.
  isCurrentAssignment?: boolean;
}) {
  const [apiKeyRef, setApiKeyRef] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const [voices, setVoices] = useState<ElevenLabsVoice[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [savingLanguage, setSavingLanguage] = useState(false);
  const [languageError, setLanguageError] = useState<string | null>(null);
  // null = no explicit choice yet — defaults to English when the account
  // has it, matching ElevenLabs' own agent builder ("Default language is
  // English").
  const [languageFilter, setLanguageFilter] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  // Tracks the latest `provider` prop for handleRefresh's staleness check
  // below — a plain closure over `provider` can't do this: handleRefresh is
  // re-created every render, so the closure it captures is always the same
  // snapshot its own comparison would be checked against (always equal,
  // never actually catching a stale response). A ref updated on every
  // render is the only way to compare a pending request's provider id
  // against whatever is *actually* current when it resolves.
  const currentProviderId = useRef(provider?.id);
  useEffect(() => {
    currentProviderId.current = provider?.id;
  }, [provider?.id]);

  useEffect(() => {
    if (!provider) return;
    const cached = voicesCache.get(provider.id);
    if (cached) {
      // Cache hit: adopt the list without a fetch, so `loading` (which
      // starts true) must be cleared here too — same "why" as the reset
      // below, just for the branch that skips the fetch entirely.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setVoices(cached);
      setLoading(false);
      return;
    }
    let ignore = false;
    // Reset for the real fetch below (not just initial mount): if the
    // Voice card's engine chooser switches to a different ElevenLabs
    // provider (e.g. multiple connected accounts) while this component
    // stays mounted, `provider` changes and this effect re-runs — without
    // resetting here, a stale error/stale "not loading" from the previous
    // provider would flash before the new fetch resolves.
    setLoading(true);
    setError(null);
    listElevenLabsVoices(provider.id)
      .then((v) => {
        if (ignore) return;
        voicesCache.set(provider.id, v);
        setVoices(v);
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

  const handleRefresh = () => {
    if (!provider) return;
    // Same stale-response guard as the effect above: if the Voice card
    // switches to a different ElevenLabs provider while this refresh is in
    // flight, its resolution must not clobber the new provider's state.
    // Checked against currentProviderId.current (kept fresh every render),
    // not the `provider` this closure captured — that value never changes
    // within one call of handleRefresh, so comparing against it here would
    // always be true and never actually catch a stale response.
    const providerId = provider.id;
    voicesCache.delete(providerId);
    setLoading(true);
    setError(null);
    listElevenLabsVoices(providerId)
      .then((v) => {
        voicesCache.set(providerId, v);
        if (providerId === currentProviderId.current) setVoices(v);
      })
      .catch((e) => {
        if (providerId === currentProviderId.current) setError(e instanceof ApiError ? e.detail : String(e));
      })
      .finally(() => {
        if (providerId === currentProviderId.current) setLoading(false);
      });
  };

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
            <div style={{ flex: 1 }}>
              <SecretRefInput
                value={apiKeyRef}
                onChange={setApiKeyRef}
                placeholder="env:ELEVENLABS_API_KEY"
                disabled={disabled}
              />
            </div>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={handleConnect}
              disabled={connecting || !apiKeyRef.trim() || disabled}
            >
              {connecting ? "Connecting…" : "Connect"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  const handleSynthesisLanguageChange = async (language: string) => {
    setSavingLanguage(true);
    setLanguageError(null);
    try {
      const updated = await updateProvider(provider.id, { language: language || null });
      onVoicePicked(updated);
    } catch (e) {
      setLanguageError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSavingLanguage(false);
    }
  };

  const handlePick = async (voiceId: string, language: string | null) => {
    setSaving(voiceId);
    setError(null);
    try {
      const updated = await updateProvider(provider.id, { voice: voiceId });
      onVoicePicked(updated);
      if (language) onLanguageDetected(language);
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

  // Languages come from each voice's own verified_languages — ElevenLabs
  // has no fixed list to draw from (unlike local engines' static
  // VOICES_BY_ENGINE), so the filter options are whatever languages this
  // account's voices actually have. Not every voice has been through
  // ElevenLabs' language verification, so labels.language (arbitrary,
  // unvalidated free text) is only a fallback for those.
  const languages = Array.from(new Set(voices.map(voicePrimaryLanguage).filter((l): l is string => !!l))).sort();
  // languageFilter only wins when it's still a real option — a Refresh (or
  // switching provider) can return a voice list that no longer has the
  // previously-selected language, and without this guard the filter would
  // stay stuck on a value that matches zero voices with no visible way to
  // reset it (the filter chips are hidden once only one language remains).
  const activeLanguageFilter =
    languageFilter && languages.includes(languageFilter) ? languageFilter : languages.includes("en") ? "en" : "all";
  const filteredVoices =
    activeLanguageFilter === "all" ? voices : voices.filter((v) => voicePrimaryLanguage(v) === activeLanguageFilter);
  const selectedVoice = voices.find((v) => v.voice_id === provider.voice);

  if (!expanded) {
    return (
      <button
        type="button"
        className="kb-row"
        style={{ width: "100%", textAlign: "left", cursor: disabled ? "not-allowed" : "pointer", background: "none", border: "1px solid var(--border-2)", borderRadius: "var(--rs)", opacity: disabled ? 0.6 : 1 }}
        onClick={() => !disabled && setExpanded(true)}
        disabled={disabled}
      >
        {selectedVoice ? (
          <>
            <span style={{ color: isCurrentAssignment ? "var(--green)" : "var(--text-3)", marginRight: 8 }}>
              {isCurrentAssignment ? "✓" : "•"}
            </span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>{selectedVoice.name}</div>
              <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
                {isCurrentAssignment ? "Primary voice" : "Already set on this account — not yet assigned to this agent"}
              </div>
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
          {selectedVoice
            ? isCurrentAssignment
              ? `Selected: ${selectedVoice.name}`
              : `${selectedVoice.name} is set on this account — pick a voice below to assign it to this agent`
            : "Select a voice"}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button type="button" className="btn btn-ghost btn-sm" onClick={handleRefresh} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setExpanded(false)}>
            Close
          </button>
        </div>
      </div>
      {languageError && <div className="error-banner">{languageError}</div>}
      <div className="form-group" style={{ marginBottom: 12 }}>
        <label className="form-label">
          Synthesis Language <span className="hint">what language the voice actually speaks on calls — not just which voices are shown below</span>
        </label>
        <select
          className="form-select"
          value={provider.language ?? ""}
          disabled={savingLanguage || disabled}
          onChange={(e) => handleSynthesisLanguageChange(e.target.value)}
        >
          <option value="">Auto-detect from text (default)</option>
          {ELEVENLABS_LANGUAGES.map((l) => (
            <option key={l.value} value={l.value}>
              {l.label}
            </option>
          ))}
        </select>
      </div>
      {languages.length > 1 && (
        <div style={{ marginBottom: 12 }}>
          <div className="form-label" style={{ marginBottom: 6 }}>
            Filter voices below by native language/accent
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
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
                  {voicePrimaryLanguage(v) ? ` · ${voicePrimaryLanguage(v)}` : ""}
                </div>
              </div>
              {v.preview_url && <audio controls src={v.preview_url} style={{ height: 30, maxWidth: 200 }} />}
              <button
                type="button"
                className={`btn btn-sm ${isSelected ? "btn-primary" : "btn-ghost"}`}
                onClick={() => handlePick(v.voice_id, voicePrimaryLanguage(v))}
                disabled={saving !== null || disabled}
              >
                {isSaving ? "…" : isSelected ? "Selected" : "Select"}
              </button>
            </div>
          );
        })}
      </div>
      <div style={{ fontSize: ".7rem", color: "var(--text-3)", marginTop: 10 }}>
        Voices come from this ElevenLabs account directly, and are cached after the first load — add or remove voices at elevenlabs.io, then click Refresh above to see the change here.
      </div>
    </div>
  );
}
