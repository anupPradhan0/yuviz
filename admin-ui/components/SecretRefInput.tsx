"use client";

import { useState } from "react";

// A ref string ("vault:path#field", "env:VAR_NAME") is never the resolved
// secret itself — safe to store/return from the API (see services/config/
// provider_configs.py's module docstring) — but it still visually reads as
// a credential field to anyone glancing at the screen. Masked by default,
// same as a password input, with a Show toggle to verify what was typed
// (these ref strings are easy to typo and worth being able to check).
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

  return (
    <div style={{ display: "flex", gap: 8 }}>
      <input
        className="form-input"
        style={{ fontFamily: "var(--mono)" }}
        type={visible ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
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
  );
}
