"use client";

import { useState } from "react";

// A key ("AIza...", encrypted server-side into enc:...) and a pointer
// ("env:GEMINI_API_KEY") wear the same shape, and conflating them stored a
// live key in plaintext. Pasting a key is the default; the
// pointer is behind a link for deployments with a secret manager.

const isStored = (v: string) => v.startsWith("enc:");

export function SecretRefInput({
  value,
  onChange,
  placeholder,
  disabled = false,
  canEncrypt = false,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Only true where the caller sends the value through secretPayload() to
   *  an `api_key` field. Everywhere else it lands in api_key_ref unencrypted,
   *  so offering "paste the key" there would store one in plaintext. */
  canEncrypt?: boolean;
}) {
  const [visible, setVisible] = useState(false);
  // A saved key stays hidden until Replace, so editing anything else on the
  // provider doesn't mean retyping the credential.
  const [replacing, setReplacing] = useState(false);
  const [mode, setMode] = useState<"key" | "ref">(
    canEncrypt && (!value || isStored(value)) ? "key" : "ref",
  );

  // Only clear on the first keystroke, so clicking Replace and then saving
  // something else leaves the stored credential alone.
  const replace = (v: string) => onChange(v);

  const trimmed = value.trim();
  let warning: string | null = null;
  if (trimmed && !isStored(value)) {
    if (mode === "key" && /\s/.test(trimmed)) {
      // The one shape a real key never has — the classic mistake is
      // pasting a whole header line ("Authorization: Bearer sk-...")
      // instead of just the token.
      warning = 'This looks like it includes extra text, not just the key — e.g. paste "sk-..." alone, not "Authorization: Bearer sk-...".';
    } else if (mode === "ref" && !/^(env|k8s):\S+$/i.test(trimmed)) {
      warning = 'This doesn\'t look like a pointer — use env:VAR_NAME or k8s:namespace/secret.';
    }
  }

  if (isStored(value) && !replacing) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="badge green">Key saved</span>
        <span className="form-hint" style={{ marginTop: 0 }}>
          Encrypted. It can&apos;t be shown again.
        </span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={disabled}
            onClick={() => {
              if (confirm("Replace the saved key? The old one is discarded as soon as you save the new one.")) {
                setReplacing(true);
              }
            }}
          >
            Replace
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={disabled}
            onClick={() => {
              if (confirm("Remove this key? Whatever uses it will stop working until you add a replacement and save.")) {
                onChange("");
              }
            }}
          >
            Remove
          </button>
        </span>
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
          value={isStored(value) ? "" : value}
          onChange={(e) => replace(e.target.value)}
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
      {warning && (
        <div className="form-hint" style={{ color: "var(--amber)" }}>
          {warning}
        </div>
      )}
      <div className="form-hint">
        {mode === "key" && canEncrypt ? (
          <>
            Paste the key from your provider. It&apos;s encrypted before it&apos;s stored, and
            never shown again.{" "}
            <button type="button" className="wf-linkish" onClick={() => { setMode("ref"); onChange(""); }}>
              Use an environment variable instead
            </button>
          </>
        ) : (
          <>
            Point at a secret provisioned elsewhere — <code>env:VAR_NAME</code>{" "}
            or <code>k8s:namespace/secret</code>. The variable has to
            be set on the conversation service.{" "}
            {canEncrypt && (
              <button type="button" className="wf-linkish" onClick={() => { setMode("key"); onChange(""); }}>
                Paste the key instead
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** A pointer goes to api_key_ref verbatim; anything else is a credential and
 *  goes to api_key for the server to encrypt. */
export function secretPayload(
  value: string,
  original = "",
): { api_key_ref?: string; api_key?: string } {
  const v = value.trim();
  // Empty clears only when there was something to clear (the Remove button);
  // otherwise the field is untouched and must not be sent.
  if (!v) return original ? { api_key_ref: "" } : {};
  // Anything scheme-shaped goes to api_key_ref, including a miscased or
  // unknown scheme: the server rejects those with an actionable message,
  // whereas treating "ENV:FOO" as a credential would encrypt the pointer and
  // surface as a vendor 401 at call time instead. Requiring no whitespace
  // anywhere is what keeps this from also catching a pasted header line
  // like "Authorization: Bearer sk-..." — every real ref is one unbroken
  // token, but that mistake has a space right after its colon.
  if (/^[A-Za-z0-9_]+:\S+$/.test(v)) return { api_key_ref: v };
  return { api_key: v };
}
