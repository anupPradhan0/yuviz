"use client";

import { useRef } from "react";

interface Props {
  value: string;
  onChange: (value: string) => void;
  length?: number;
  disabled?: boolean;
}

// One box per digit; typing advances, Backspace steps back, pasting fills all.
export function CodeInput({ value, onChange, length = 6, disabled }: Props) {
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  const digits = Array.from({ length }, (_, i) => value[i] ?? "");

  const fill = (start: number, text: string) => {
    const incoming = text.replace(/\D/g, "");
    if (!incoming) return;
    const next = [...digits];
    for (let i = 0; i < incoming.length && start + i < length; i++) next[start + i] = incoming[i];
    onChange(next.join(""));
    refs.current[Math.min(start + incoming.length, length - 1)]?.focus();
  };

  const onKeyDown = (i: number, e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Backspace" && !digits[i] && i > 0) {
      e.preventDefault();
      const next = [...digits];
      next[i - 1] = "";
      onChange(next.join(""));
      refs.current[i - 1]?.focus();
    } else if (e.key === "ArrowLeft" && i > 0) {
      refs.current[i - 1]?.focus();
    } else if (e.key === "ArrowRight" && i < length - 1) {
      refs.current[i + 1]?.focus();
    }
  };

  return (
    <div className="code-input" role="group" aria-label="Verification code">
      {digits.map((d, i) => (
        <input
          key={i}
          ref={(el) => { refs.current[i] = el; }}
          className="code-input-box"
          inputMode="numeric"
          autoComplete={i === 0 ? "one-time-code" : "off"}
          aria-label={`Digit ${i + 1}`}
          maxLength={length}
          value={d}
          disabled={disabled}
          autoFocus={i === 0}
          onChange={(e) => {
            let text = e.target.value;
            if (!text) {
              const next = [...digits];
              next[i] = "";
              onChange(next.join(""));
              return;
            }
            // Typed into a filled box: keep only the new digit.
            if (d && text.length === 2) text = text.startsWith(d) ? text.slice(1) : text.slice(0, 1);
            fill(i, text);
          }}
          onPaste={(e) => {
            e.preventDefault();
            fill(i, e.clipboardData.getData("text"));
          }}
          onKeyDown={(e) => onKeyDown(i, e)}
          onFocus={(e) => e.target.select()}
        />
      ))}
    </div>
  );
}
