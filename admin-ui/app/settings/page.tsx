"use client";

import { useEffect, useState } from "react";
import { ApiError, changePassword, getCurrentUser, User } from "@/lib/api";

type SettingsSection = "profile" | "sessions" | "security";

const YOUR_ACCOUNT: { id: SettingsSection; label: string }[] = [
  { id: "profile", label: "Profile" },
  { id: "sessions", label: "Sessions" },
  { id: "security", label: "Security" },
];

function ProfilePanel() {
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    getCurrentUser().then(setUser).catch(() => {});
  }, []);

  return (
    <div className="card">
      <div className="card-hdr">
        <div className="card-title">Account Details</div>
      </div>
      <div className="card-body">
        <div className="form-row">
          <div className="form-group">
            <label className="form-label">Email</label>
            <input className="form-input" value={user?.email ?? "…"} disabled />
          </div>
          <div className="form-group">
            <label className="form-label">Role</label>
            <input className="form-input" value={user?.role ?? "…"} disabled />
          </div>
        </div>
        <div className="form-hint">
          Editing your own email/role isn&apos;t supported yet — a superadmin can update it via the Config Service&apos;s Users API.
        </div>
      </div>
    </div>
  );
}

function SessionsPanel() {
  return (
    <div className="card">
      <div className="card-hdr">
        <div className="card-title">Signed-in Devices</div>
        <div className="card-sub">Sign out anything you don&apos;t recognize</div>
      </div>
      <div className="card-body">
        <div className="health-row">
          <span className="status-dot green"></span>
          <div>
            <div className="health-name">
              This browser <span className="badge green" style={{ marginLeft: 6 }}>This device</span>
            </div>
            <div className="health-engine">Localhost · last active just now</div>
          </div>
        </div>
        <div className="form-hint" style={{ marginTop: 8 }}>
          Session tracking isn&apos;t backed by a real auth service yet — this shows only the local browser session.
        </div>
      </div>
    </div>
  );
}

function SecurityPanel() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(false);
    if (newPassword.length < 8) {
      setError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation don't match.");
      return;
    }
    setSubmitting(true);
    try {
      await changePassword(currentPassword, newPassword);
      setSuccess(true);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-hdr">
          <div className="card-title">Two-Factor Authentication</div>
        </div>
        <div className="card-body">
          <label className="toggle">
            <input type="checkbox" disabled />
            <span className="toggle-track">
              <span className="toggle-thumb" />
            </span>
            <span style={{ fontSize: ".78rem", color: "var(--text-2)", marginLeft: 9 }}>Not available yet</span>
          </label>
        </div>
      </div>
      <div className="card">
        <div className="card-hdr">
          <div className="card-title">Change Password</div>
          <div className="card-sub">Use at least 8 characters</div>
        </div>
        <div className="card-body">
          {error && <div className="error-banner">{error}</div>}
          {success && (
            <div className="form-hint" style={{ color: "var(--green)", marginBottom: 12 }}>
              Password changed.
            </div>
          )}
          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label className="form-label">Current Password</label>
              <input
                className="form-input"
                type="password"
                placeholder="Your current password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                required
              />
            </div>
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">New Password</label>
                <input
                  className="form-input"
                  type="password"
                  placeholder="At least 8 characters"
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  required
                />
              </div>
              <div className="form-group">
                <label className="form-label">Confirm New Password</label>
                <input
                  className="form-input"
                  type="password"
                  placeholder="Repeat new password"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  required
                />
              </div>
            </div>
            <button className="btn btn-primary btn-sm" type="submit" disabled={submitting}>
              {submitting ? "Changing…" : "Change Password"}
            </button>
          </form>
        </div>
      </div>
    </>
  );
}

export default function SettingsPage() {
  const [section, setSection] = useState<SettingsSection>("profile");

  return (
    <div className="cols">
      <div style={{ width: 190, flexShrink: 0 }}>
        <div className="card" style={{ padding: "8px 6px" }}>
          <div className="nav-section" style={{ paddingTop: 6 }}>
            Your Account
          </div>
          {YOUR_ACCOUNT.map((item) => (
            <div
              key={item.id}
              className={`nav-item${section === item.id ? " active" : ""}`}
              onClick={() => setSection(item.id)}
              style={{ cursor: "pointer" }}
            >
              {item.label}
            </div>
          ))}
        </div>
      </div>
      <div className="col-main">
        {section === "profile" && <ProfilePanel />}
        {section === "sessions" && <SessionsPanel />}
        {section === "security" && <SecurityPanel />}
      </div>
    </div>
  );
}
