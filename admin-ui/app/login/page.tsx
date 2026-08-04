"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, login } from "@/lib/api";
import { setToken } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const result = await login(email, password);
      setToken(result.access_token);
      router.push("/tenants");
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">
          <div className="login-logo-icon">
            <svg width="20" height="18" viewBox="0 0 18 16" fill="none">
              <rect x="0" y="6" width="2.5" height="4" rx="1.25" fill="currentColor" />
              <rect x="3.75" y="3.5" width="2.5" height="9" rx="1.25" fill="currentColor" />
              <rect x="7.5" y="0" width="3" height="16" rx="1.5" fill="currentColor" />
              <rect x="11.75" y="3.5" width="2.5" height="9" rx="1.25" fill="currentColor" />
              <rect x="15.5" y="5.5" width="2.5" height="5" rx="1.25" fill="currentColor" />
            </svg>
          </div>
          <div className="login-logo-text">
            Yuviz<span>.ai</span>
          </div>
        </div>
        <div className="login-box">
          <div className="login-title">Sign in to your console</div>
          <div className="login-sub">Manage agents, providers, and calls.</div>
          {error && <div className="error-banner">{error}</div>}
          <form onSubmit={handleSubmit}>
            <div className="login-field">
              <label className="login-label">Email</label>
              <input
                className="login-input"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="login-field">
              <label className="login-label">Password</label>
              <input
                className="login-input"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            <button className="login-btn" type="submit" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign In"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
