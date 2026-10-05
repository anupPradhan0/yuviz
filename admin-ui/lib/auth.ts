// Config Service JWT storage. localStorage + bearer header, not a cookie, since the
// API is cross-origin and this avoids SameSite/CSRF handling.

const TOKEN_KEY = "yuviz_access_token";

export interface StoredUser {
  id: string;
  email: string;
  role: "superadmin" | "admin" | "viewer";
  tenant_id: string | null;
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

// Unverified `sub` claim: only for scoping browser-local data, never for authorization.
export function tokenUserId(): string | null {
  const payload = getToken()?.split(".")[1];
  if (!payload) return null;
  try {
    const sub = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/"))).sub;
    return typeof sub === "string" && sub ? sub : null;
  } catch {
    return null;
  }
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}
