"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  changePassword,
  createUser,
  deleteUser,
  getCurrentUser,
  listTenants,
  listUsers,
  Tenant,
  updateUser,
  User,
  UserRole,
  UserUpdate,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

type SettingsSection = "profile" | "sessions" | "security" | "users";

const YOUR_ACCOUNT: { id: SettingsSection; label: string }[] = [
  { id: "profile", label: "Profile" },
  { id: "sessions", label: "Sessions" },
  { id: "security", label: "Security" },
];

const ORGANIZATION: { id: SettingsSection; label: string }[] = [
  { id: "users", label: "Users" },
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

const ROLE_BADGE: Record<UserRole, string> = { superadmin: "red", admin: "indigo", viewer: "gray" };

function UsersPanel() {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<User | null>(null);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("admin");
  const [tenantId, setTenantId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [editForm, setEditForm] = useState<UserUpdate>({});
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const isSuperadmin = currentUser?.role === "superadmin";
  const canCreate = currentUser?.role === "superadmin" || currentUser?.role === "admin";

  const refresh = () => {
    setLoading(true);
    Promise.all([getCurrentUser(), listUsers()])
      .then(async ([me, us]) => {
        setCurrentUser(me);
        setUsers(us);
        if (me.role === "superadmin") setTenants(await listTenants());
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, []);

  const tenantName = (id: string | null) => (id ? tenants.find((t) => t.id === id)?.name ?? id : "— platform —");

  const openCreate = () => {
    setEmail("");
    setPassword("");
    setRole("admin");
    setTenantId(isSuperadmin ? "" : currentUser?.tenant_id ?? "");
    setFormError(null);
    setModalOpen(true);
  };

  const handleCreate = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      // Non-superadmins are pinned to their own tenant_id here — the backend
      // trusts whatever tenant_id/role the caller sends (create_user() has
      // no escalation check of its own), so this is a real guard, not a
      // convenience default.
      await createUser({
        email,
        password,
        role,
        tenant_id: isSuperadmin ? tenantId || null : currentUser?.tenant_id ?? null,
      });
      setModalOpen(false);
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const openEdit = (u: User) => {
    setEditTarget(u);
    setEditForm({ role: u.role, tenant_id: u.tenant_id });
    setEditError(null);
  };

  const handleEditSave = async () => {
    if (!editTarget) return;
    // Editing your own row out of superadmin removes the Edit/Deactivate
    // actions from every row, including your own, for the rest of this
    // session — the same permission check that hides them for any other
    // admin/viewer. Not blocked outright (you may genuinely be handing off
    // control to another superadmin), but confirmed explicitly so it can't
    // happen as a stray click.
    if (
      editTarget.id === currentUser?.id &&
      currentUser?.role === "superadmin" &&
      editForm.role !== "superadmin" &&
      !window.confirm(
        "This removes your own superadmin access. You won't be able to manage users again until another superadmin restores it. Continue?",
      )
    ) {
      return;
    }
    setEditSubmitting(true);
    setEditError(null);
    try {
      await updateUser(editTarget.id, editForm);
      setEditTarget(null);
      refresh();
    } catch (e) {
      setEditError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setEditSubmitting(false);
    }
  };

  const handleDeactivate = async (u: User) => {
    if (!window.confirm(`Deactivate ${u.email}? They will no longer be able to sign in.`)) return;
    try {
      await deleteUser(u.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  // The create form never offers "superadmin" to a non-superadmin creator —
  // same reasoning as the tenant_id pin above: nothing server-side stops it.
  const createRoleOptions: UserRole[] = isSuperadmin ? ["superadmin", "admin", "viewer"] : ["admin", "viewer"];

  return (
    <>
      {canCreate && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14 }}>
          <button className="btn btn-primary btn-sm" onClick={openCreate}>
            + New User
          </button>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : users.length === 0 ? (
          <div className="empty-state">No users found.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Email</th>
                <th>Role</th>
                {isSuperadmin && <th>Tenant</th>}
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td className="bold">
                    {u.email}
                    {u.id === currentUser?.id && (
                      <span className="badge indigo" style={{ marginLeft: 6 }}>
                        You
                      </span>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${ROLE_BADGE[u.role]}`}>{u.role}</span>
                  </td>
                  {isSuperadmin && <td>{tenantName(u.tenant_id)}</td>}
                  <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
                    {new Date(u.created_at).toLocaleDateString()}
                  </td>
                  <td style={{ display: "flex", gap: 6 }}>
                    {isSuperadmin ? (
                      <>
                        <button className="btn btn-ghost btn-sm" onClick={() => openEdit(u)}>
                          Edit
                        </button>
                        <button
                          className="btn btn-danger btn-sm"
                          onClick={() => handleDeactivate(u)}
                          disabled={u.id === currentUser?.id}
                          title={u.id === currentUser?.id ? "You can't deactivate your own account" : undefined}
                        >
                          Deactivate
                        </button>
                      </>
                    ) : (
                      <span style={{ fontSize: ".71rem", color: "var(--text-3)" }}>—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!isSuperadmin && !loading && users.length > 0 && (
          <div className="form-hint" style={{ padding: "0 16px 16px" }}>
            Editing roles or deactivating users requires a superadmin.
          </div>
        )}
      </div>

      <Modal
        open={modalOpen}
        title="New User"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setModalOpen(false)}>
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={handleCreate}
              disabled={submitting || !email || password.length < 8}
            >
              {submitting ? "Creating…" : "Create User"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label">
            Email <span className="required">*</span>
          </label>
          <input
            className="form-input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="teammate@company.com"
          />
        </div>
        <div className="form-group">
          <label className="form-label">
            Password <span className="required">*</span>
            <span className="hint">at least 8 characters</span>
          </label>
          <input
            className="form-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
          />
        </div>
        <div className="form-group">
          <label className="form-label">Role</label>
          <select className="form-input" value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
            {createRoleOptions.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        {isSuperadmin && (
          <div className="form-group">
            <label className="form-label">
              Tenant <span className="hint">blank = platform-wide (superadmin scope)</span>
            </label>
            <select className="form-input" value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
              <option value="">— Platform (no tenant) —</option>
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </Modal>

      <Modal
        open={editTarget !== null}
        title={`Edit User — ${editTarget?.email ?? ""}`}
        onClose={() => setEditTarget(null)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setEditTarget(null)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleEditSave} disabled={editSubmitting}>
              {editSubmitting ? "Saving…" : "Save Changes"}
            </button>
          </>
        }
      >
        {editError && <div className="error-banner">{editError}</div>}
        <div className="form-group">
          <label className="form-label">Role</label>
          <select
            className="form-input"
            value={editForm.role ?? ""}
            onChange={(e) => setEditForm({ ...editForm, role: e.target.value as UserRole })}
          >
            {(["superadmin", "admin", "viewer"] as UserRole[]).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            Tenant <span className="hint">blank = platform-wide (superadmin scope)</span>
          </label>
          <select
            className="form-input"
            value={editForm.tenant_id ?? ""}
            onChange={(e) => setEditForm({ ...editForm, tenant_id: e.target.value || null })}
          >
            <option value="">— Platform (no tenant) —</option>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
      </Modal>
    </>
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
          <div className="nav-section" style={{ paddingTop: 6 }}>
            Organization
          </div>
          {ORGANIZATION.map((item) => (
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
        {section === "users" && <UsersPanel />}
      </div>
    </div>
  );
}
