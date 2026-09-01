"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  createProvider,
  deleteProvider,
  listProviders,
  listTenants,
  ProviderConfig,
  ProviderConfigCreate,
  ProviderConfigUpdate,
  ProviderEnvironment,
  ProviderRole,
  Tenant,
  updateProvider,
} from "@/lib/api";
import { Modal } from "@/components/Modal";
import { SecretRefInput, secretPayload } from "./SecretRefInput";
import { EMBEDDING_MODELS_BY_ENGINE, ENGINES_BY_ROLE, MODELS_BY_ENGINE, OTHER, VOICES_BY_ENGINE } from "@/lib/engineCatalog";

const ALL_ROLES: ProviderRole[] = ["stt", "llm", "tts"];
const ENVIRONMENTS: ProviderEnvironment[] = ["prod", "staging", "dev"];

const emptyForm = (defaultRole: ProviderRole): ProviderConfigCreate => ({
  name: "",
  role: defaultRole,
  engine: ENGINES_BY_ROLE[defaultRole][0].value,
  environment: "prod",
});

export function ProvidersPanel({ allowedRoles = ALL_ROLES, title }: { allowedRoles?: ProviderRole[]; title?: string }) {
  const ROLES = allowedRoles;
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState<string>("");
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ProviderConfig | null>(null);

  const [form, setForm] = useState<ProviderConfigCreate>(emptyForm(allowedRoles[0]));
  const [modelChoice, setModelChoice] = useState<string>("");
  const [customModel, setCustomModel] = useState("");
  const [voiceChoice, setVoiceChoice] = useState<string>("");
  const [customVoice, setCustomVoice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    listTenants().then((ts) => {
      setTenants(ts);
      if (ts.length > 0) setTenantId(ts[0].id);
    });
  }, []);

  const refresh = () => {
    if (!tenantId) return;
    setLoading(true);
    listProviders(tenantId)
      .then((all) => setProviders(all.filter((p) => allowedRoles.includes(p.role))))
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps, react-hooks/set-state-in-effect
  useEffect(refresh, [tenantId]);

  const resetForm = () => {
    setForm(emptyForm(allowedRoles[0]));
    setModelChoice("");
    setCustomModel("");
    setVoiceChoice("");
    setCustomVoice("");
    setEditing(null);
  };

  const handleRoleChange = (role: ProviderRole) => {
    setForm({ ...form, role, engine: ENGINES_BY_ROLE[role][0].value });
    setModelChoice("");
    setCustomModel("");
    setVoiceChoice("");
    setCustomVoice("");
  };

  const handleEngineChange = (engine: string) => {
    setForm({ ...form, engine });
    setModelChoice("");
    setCustomModel("");
    setVoiceChoice("");
    setCustomVoice("");
  };

  const openCreate = () => {
    resetForm();
    setModalOpen(true);
  };

  const openEdit = (p: ProviderConfig) => {
    setEditing(p);
    setForm({ name: p.name, role: p.role, engine: p.engine, environment: p.environment, api_key_ref: p.api_key_ref || undefined });
    const models = MODELS_BY_ENGINE[p.engine];
    if (p.model) setModelChoice(models && models.includes(p.model) ? p.model : OTHER);
    if (p.model && !(models && models.includes(p.model))) setCustomModel(p.model);
    const voices = VOICES_BY_ENGINE[p.engine];
    const knownVoice = voices?.some((v) => v.id === p.voice) ?? false;
    if (p.voice) setVoiceChoice(knownVoice ? p.voice : OTHER);
    if (p.voice && !knownVoice) setCustomVoice(p.voice);
    setModalOpen(true);
  };

  const handleSubmit = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      const model = modelChoice === OTHER ? customModel : modelChoice || undefined;
      const voice = voiceChoice === OTHER ? customVoice : voiceChoice || undefined;
      if (editing) {
        const body: ProviderConfigUpdate = {
          name: form.name,
          engine: form.engine,
          environment: form.environment,
          model,
          voice,
          // A pasted key goes to `api_key` (encrypted server-side); a
          // pointer goes to api_key_ref verbatim. See secretPayload.
          ...secretPayload(form.api_key_ref || ""),
        };
        await updateProvider(editing.id, body);
      } else {
        const { api_key_ref: typed, ...rest } = form;
        await createProvider(tenantId, { ...rest, model, voice, ...secretPayload(typed || "") });
      }
      setModalOpen(false);
      resetForm();
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (p: ProviderConfig) => {
    if (!confirm(`Delete provider "${p.name}"? This cannot be undone.`)) return;
    try {
      await deleteProvider(p.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const modelOptions = form.role === "embedding" ? EMBEDDING_MODELS_BY_ENGINE[form.engine] : MODELS_BY_ENGINE[form.engine];
  const voiceOptions = VOICES_BY_ENGINE[form.engine];

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14, gap: 10 }}>
        <select className="form-select" style={{ width: 240 }} value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
        <button className="btn btn-primary btn-sm" onClick={openCreate} disabled={!tenantId}>
          + New Provider
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {title && (
          <div className="card-hdr">
            <div className="card-title">{title}</div>
          </div>
        )}
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : providers.length === 0 ? (
          <div className="empty-state">No providers configured for this tenant yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                {ROLES.length > 1 && <th>Role</th>}
                <th>Engine</th>
                <th>Model / Voice</th>
                <th>Environment</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => (
                <tr key={p.id}>
                  <td className="bold">{p.name}</td>
                  {ROLES.length > 1 && (
                    <td>
                      <span className="badge indigo">{p.role.toUpperCase()}</span>
                    </td>
                  )}
                  <td className="mono">{p.engine}</td>
                  <td className="mono">{p.model || p.voice || "—"}</td>
                  <td>
                    <span className={`env-chip ${p.environment}`}>{p.environment}</span>
                  </td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => openEdit(p)}>
                      Edit
                    </button>{" "}
                    <button className="btn btn-danger btn-sm" onClick={() => handleDelete(p)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal
        open={modalOpen}
        title={editing ? `Edit Provider — ${editing.name}` : "New Provider Config"}
        onClose={() => {
          setModalOpen(false);
          resetForm();
        }}
        footer={
          <>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setModalOpen(false);
                resetForm();
              }}
            >
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={handleSubmit}
              disabled={submitting || !form.name || !form.engine}
            >
              {submitting ? "Saving…" : editing ? "Save Changes" : "Create Provider"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label">
            Name <span className="required">*</span>
          </label>
          <input className="form-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Deepgram Nova-3" />
        </div>
        <div className="form-row">
          {ROLES.length > 1 && (
            <div className="form-group">
              <label className="form-label">
                Role {editing && <span className="hint">(fixed after creation)</span>}
              </label>
              <select
                className="form-select"
                value={form.role}
                onChange={(e) => handleRoleChange(e.target.value as ProviderRole)}
                disabled={!!editing}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r.toUpperCase()}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="form-group">
            <label className="form-label">Environment</label>
            <select
              className="form-select"
              value={form.environment}
              onChange={(e) => setForm({ ...form, environment: e.target.value as ProviderEnvironment })}
            >
              {ENVIRONMENTS.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="form-group">
          <label className="form-label">
            Engine <span className="required">*</span>
          </label>
          <select className="form-select" value={form.engine} onChange={(e) => handleEngineChange(e.target.value)}>
            {ENGINES_BY_ROLE[form.role].map((eng) => (
              <option key={eng.value} value={eng.value}>
                {eng.label}
              </option>
            ))}
          </select>
        </div>

        {(form.role === "stt" || form.role === "llm" || form.role === "embedding") && (
          <div className="form-group">
            <label className="form-label">
              Model {form.role === "embedding" && <span className="hint">leave unset to use the engine&apos;s default</span>}
            </label>
            {modelOptions ? (
              <>
                <select className="form-select" value={modelChoice} onChange={(e) => setModelChoice(e.target.value)}>
                  <option value="">— select a model —</option>
                  {modelOptions.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                  <option value={OTHER}>Other (custom)…</option>
                </select>
                {modelChoice === OTHER && (
                  <input
                    className="form-input"
                    style={{ marginTop: 6, fontFamily: "var(--mono)" }}
                    value={customModel}
                    onChange={(e) => setCustomModel(e.target.value)}
                    placeholder="custom model name"
                  />
                )}
              </>
            ) : (
              <input className="form-input" style={{ fontFamily: "var(--mono)" }} value={customModel} onChange={(e) => setCustomModel(e.target.value)} placeholder="model name" />
            )}
          </div>
        )}

        {form.role === "tts" && (
          <div className="form-group">
            <label className="form-label">
              Voice {form.engine === "elevenlabs" && <span className="hint">account-specific voice_id — enter your own</span>}
            </label>
            {voiceOptions ? (
              <>
                <select className="form-select" value={voiceChoice} onChange={(e) => setVoiceChoice(e.target.value)}>
                  <option value="">— select a voice —</option>
                  {voiceOptions.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label} ({v.gender})
                    </option>
                  ))}
                  <option value={OTHER}>Other (custom)…</option>
                </select>
                {voiceChoice === OTHER && (
                  <input
                    className="form-input"
                    style={{ marginTop: 6, fontFamily: "var(--mono)" }}
                    value={customVoice}
                    onChange={(e) => setCustomVoice(e.target.value)}
                    placeholder="custom voice name"
                  />
                )}
              </>
            ) : (
              <input
                className="form-input"
                style={{ fontFamily: "var(--mono)" }}
                value={customVoice}
                onChange={(e) => setCustomVoice(e.target.value)}
                placeholder="e.g. 21m00Tcm4TlvDq8ikWAM"
              />
            )}
          </div>
        )}

        <div className="form-group">
          <label className="form-label">
            API Key Ref{" "}
            <span className="hint">paste it — we encrypt it</span>
          </label>
          <SecretRefInput
            value={form.api_key_ref || ""}
            onChange={(v) => setForm({ ...form, api_key_ref: v })}
          />
        </div>
      </Modal>
    </>
  );
}
