"use client";

import { useEffect, useState } from "react";
import {
  ApiError,
  AuditLogEntry,
  changePassword,
  createUser,
  deleteUser,
  getCurrentUser,
  listAuditLog,
  listTenants,
  listUsers,
  Tenant,
  updateUser,
  User,
  UserRole,
  UserUpdate,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

type SettingsSection = "profile" | "sessions" | "security" | "users" | "audit-log";

const YOUR_ACCOUNT: { id: SettingsSection; label: string }[] = [
  { id: "profile", label: "Profile" },
  { id: "sessions", label: "Sessions" },
  { id: "security", label: "Security" },
];

const ORGANIZATION: { id: SettingsSection; label: string }[] = [
  { id: "users", label: "Users" },
  { id: "audit-log", label: "Audit Log" },
];

const ENTITY_TYPES = [
  "agent",
  "agent_tool_policy",
  "carrier",
  "phone_number",
  "provider_config",
  "telephony_config",
  "tenant",
  "tool_provider_config",
  "user",
];

const ACTION_BADGE: Record<string, string> = { created: "green", updated: "amber", deleted: "red" };

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
  const [resetPassword, setResetPassword] = useState("");
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
    setResetPassword("");
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
    if (resetPassword && resetPassword.length < 8) {
      setEditError("New password must be at least 8 characters.");
      return;
    }
    setEditSubmitting(true);
    setEditError(null);
    try {
      await updateUser(editTarget.id, { ...editForm, ...(resetPassword ? { password: resetPassword } : {}) });
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
        <div className="form-group">
          <label className="form-label">
            Reset Password <span className="hint">leave blank to keep their current one</span>
          </label>
          <input
            className="form-input"
            type="password"
            value={resetPassword}
            onChange={(e) => setResetPassword(e.target.value)}
            placeholder="At least 8 characters"
            autoComplete="new-password"
          />
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

function AuditDiff({ oldValue, newValue }: { oldValue: string | null; newValue: string | null }) {
  const parse = (v: string | null) => {
    if (!v) return null;
    try {
      return JSON.parse(v) as Record<string, unknown>;
    } catch {
      return null;
    }
  };
  const before = parse(oldValue);
  const after = parse(newValue);

  // Only the fields that actually changed — a full before/after dump of
  // every column buries the one or two that matter on every "updated" row.
  // Redacted fields (api_key_ref/auth_token_ref/password_hash — see
  // audit.py's _redact()) are the one exception: "[redacted]" reads as
  // equal to "[redacted]" even when the real underlying values genuinely
  // differed, so equality here can never actually rule out a change —
  // always show them rather than silently dropping a real password/secret
  // change because its redacted display happens to match on both sides.
  const keys = new Set([...(before ? Object.keys(before) : []), ...(after ? Object.keys(after) : [])]);
  const changed = [...keys].filter(
    (k) => before?.[k] === "[redacted]" || after?.[k] === "[redacted]" || JSON.stringify(before?.[k]) !== JSON.stringify(after?.[k]),
  );

  if (changed.length === 0) {
    return <div className="form-hint">No field-level diff available.</div>;
  }

  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Field</th>
          <th>Before</th>
          <th>After</th>
        </tr>
      </thead>
      <tbody>
        {changed.map((k) => {
          const isRedacted = before?.[k] === "[redacted]" || after?.[k] === "[redacted]";
          return (
            <tr key={k}>
              <td className="mono">{k}</td>
              <td className="mono" style={{ color: "var(--text-3)" }}>
                {before ? JSON.stringify(before[k]) : "—"}
              </td>
              <td className="mono">
                {after ? JSON.stringify(after[k]) : "—"}
                {isRedacted && (
                  <div style={{ fontSize: ".68rem", color: "var(--text-3)", fontFamily: "var(--font)" }}>
                    value changed — redacted, can&apos;t show what
                  </div>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function AuditLogPanel() {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditLogEntry | null>(null);

  const [entityType, setEntityType] = useState("");
  const [action, setAction] = useState("");
  const [userEmail, setUserEmail] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 25;

  const isSuperadmin = currentUser?.role === "superadmin";

  const refresh = () => {
    setLoading(true);
    getCurrentUser()
      .then(async (me) => {
        setCurrentUser(me);
        if (me.role !== "superadmin") return;
        const result = await listAuditLog({
          entity_type: entityType || undefined,
          action: (action || undefined) as AuditLogEntry["action"] | undefined,
          user_email: userEmail || undefined,
          limit,
          offset,
        });
        setEntries(result.items);
        setTotal(result.total);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, [entityType, action, userEmail, offset]);

  if (currentUser && !isSuperadmin) {
    return (
      <div className="card">
        <div className="card-body">
          <div className="empty-state">Viewing the audit log requires a superadmin.</div>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-body" style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <div className="form-group" style={{ margin: 0 }}>
            <label className="form-label">Entity Type</label>
            <select
              className="form-input"
              value={entityType}
              onChange={(e) => {
                setOffset(0);
                setEntityType(e.target.value);
              }}
            >
              <option value="">All</option>
              {ENTITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="form-group" style={{ margin: 0 }}>
            <label className="form-label">Action</label>
            <select
              className="form-input"
              value={action}
              onChange={(e) => {
                setOffset(0);
                setAction(e.target.value);
              }}
            >
              <option value="">All</option>
              <option value="created">created</option>
              <option value="updated">updated</option>
              <option value="deleted">deleted</option>
            </select>
          </div>
          <div className="form-group" style={{ margin: 0, flex: 1, minWidth: 200 }}>
            <label className="form-label">User Email</label>
            <input
              className="form-input"
              value={userEmail}
              onChange={(e) => {
                setOffset(0);
                setUserEmail(e.target.value);
              }}
              placeholder="search by email…"
            />
          </div>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : entries.length === 0 ? (
          <div className="empty-state">No matching audit entries.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>When</th>
                <th>Entity</th>
                <th>Action</th>
                <th>By</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>
                    {new Date(e.changed_at).toLocaleString()}
                  </td>
                  <td>
                    <span className="mono">{e.entity_type}</span>
                    <span style={{ color: "var(--text-3)", fontSize: ".7rem", marginLeft: 6 }}>
                      {e.entity_id.slice(0, 8)}…
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${ACTION_BADGE[e.action] ?? "gray"}`}>{e.action}</span>
                  </td>
                  <td>{e.user_email ?? <span style={{ color: "var(--text-3)" }}>system</span>}</td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => setDetail(e)}>
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {total > limit && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
          <span style={{ fontSize: ".75rem", color: "var(--text-3)" }}>
            {offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              className="btn btn-ghost btn-sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              Previous
            </button>
            <button
              className="btn btn-ghost btn-sm"
              disabled={offset + limit >= total}
              onClick={() => setOffset(offset + limit)}
            >
              Next
            </button>
          </div>
        </div>
      )}

      <Modal
        open={detail !== null}
        title={detail ? `${detail.entity_type} ${detail.action} — ${detail.entity_id.slice(0, 8)}…` : ""}
        onClose={() => setDetail(null)}
        footer={
          <button className="btn btn-ghost btn-sm" onClick={() => setDetail(null)}>
            Close
          </button>
        }
      >
        {detail && (
          <>
            <div className="form-hint" style={{ marginBottom: 10 }}>
              {detail.user_email ?? "system"} · {new Date(detail.changed_at).toLocaleString()}
              {detail.ip_address ? ` · ${detail.ip_address}` : ""}
            </div>
            <AuditDiff oldValue={detail.old_value} newValue={detail.new_value} />
          </>
        )}
      </Modal>
    </>
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
        {section === "audit-log" && <AuditLogPanel />}
      </div>
    </div>
  );
}
