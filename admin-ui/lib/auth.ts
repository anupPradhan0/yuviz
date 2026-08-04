// Token storage for the Config Service's JWT (see services/config/auth.py).
// localStorage, not a cookie: this is a cross-origin SPA (admin-ui at :3000,
// Config Service at :8000) talking to the API directly via fetch, not
// through a same-origin proxy — a bearer token in an Authorization header
// avoids SameSite/CSRF complexity a cookie would need for this shape.

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

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}
