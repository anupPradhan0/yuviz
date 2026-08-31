"use client";

import { useState } from "react";

// A key ("AIza...", encrypted server-side into enc:...) and a pointer
// ("env:GEMINI_API_KEY") wear the same shape, and conflating them stored a
// live key in plaintext (2026-08-28). Pasting a key is the default; the
// pointer is behind a link for deployments with a secret manager.

const isStored = (v: string) => v.startsWith("enc:");

export function SecretRefInput({
  value,
  onChange,
  placeholder,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const [visible, setVisible] = useState(false);
  // A saved key stays hidden until Replace, so editing anything else on the
  // provider doesn't mean retyping the credential.
  const [replacing, setReplacing] = useState(false);
  const [mode, setMode] = useState<"key" | "ref">(
    value && !isStored(value) ? "ref" : "key",
  );

  if (isStored(value) && !replacing) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="badge green">Key saved</span>
        <span className="form-hint" style={{ marginTop: 0 }}>
          Encrypted. It can&apos;t be shown again.
        </span>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          style={{ marginLeft: "auto" }}
          disabled={disabled}
          onClick={() => { setReplacing(true); onChange(""); }}
        >
          Replace
        </button>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          className="form-input"
          style={{ fontFamily: "var(--mono)" }}
          type={visible ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={
            placeholder ?? (mode === "key" ? "Paste the API key" : "env:MY_API_KEY")
          }
          disabled={disabled}
          autoComplete="off"
        />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => setVisible((v) => !v)}
          disabled={disabled}
        >
          {visible ? "Hide" : "Show"}
        </button>
      </div>
      <div className="form-hint">
        {mode === "key" ? (
          <>
            Paste the key from your provider. It&apos;s encrypted before it&apos;s stored, and
            never shown again.{" "}
            <button type="button" className="wf-linkish" onClick={() => { setMode("ref"); onChange(""); }}>
              Use a secret manager instead
            </button>
          </>
        ) : (
          <>
            Point at a secret provisioned elsewhere — <code>env:VAR_NAME</code>,{" "}
            <code>vault:path#field</code> or <code>k8s:namespace/secret</code>. The variable has to
            be set on the conversation service.{" "}
            <button type="button" className="wf-linkish" onClick={() => { setMode("key"); onChange(""); }}>
              Paste the key instead
            </button>
          </>
        )}
      </div>
    </div>
  );
}

/** A pointer goes to api_key_ref verbatim; anything else is a credential and
 *  goes to api_key for the server to encrypt. */
export function secretPayload(value: string): { api_key_ref?: string; api_key?: string } {
  const v = value.trim();
  // Explicit clear, not "unchanged": after Replace the field is empty, and
  // sending nothing would silently keep the credential it just cleared.
  if (!v) return { api_key_ref: "" };
  if (/^(env|vault|k8s|enc):/.test(v)) return { api_key_ref: v };
  return { api_key: v };
}
