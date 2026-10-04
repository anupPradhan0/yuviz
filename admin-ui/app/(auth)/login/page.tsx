"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import {
  ApiError, forgotPassword, getCurrentUser, googleSignInUrl, isConsoleRole, login, register,
  resendVerificationCode, resetPassword, SIGNUP_SOURCES, verifyEmail, type UserRole,
} from "@/lib/api";
import { CodeInput } from "@/components/CodeInput";
import { clearToken, setToken } from "@/lib/auth";
import { COUNTRY_DIAL_CODES } from "@/lib/countries";
import { SelectMenu } from "@/components/SelectMenu";

// Only claims the product can back up — no invented usage numbers.
const HERO = {
  signin: {
    eyebrow: "Voice AI platform",
    title: "Voice agents that listen, think and talk back",
    body: "Your AI assistant answers calls on the phone or your website, understands what callers say, replies in a natural voice, finds answers in your documents, and passes the call to a real person when needed.",
    facts: [
      { value: "Phone & web", label: "Answers calls from any phone or browser" },
      { value: "Natural voice", label: "Listens and replies instantly" },
      { value: "Private", label: "Each organization's data kept separate" },
    ],
  },
  create: {
    eyebrow: "Get started",
    title: "Set up your voice AI workspace",
    body: "Create your organization and admin account, invite your team and launch your first AI voice assistant.",
    facts: [
      { value: "01", label: "Create your organization" },
      { value: "02", label: "Connect your AI and voice services" },
      { value: "03", label: "Invite your team and go live" },
    ],
  },
};

const PROVIDERS = ["OpenAI", "Anthropic", "Gemini", "Ollama", "Deepgram", "ElevenLabs", "Whisper", "Kokoro"];

const DIAL_CODE = Object.fromEntries(COUNTRY_DIAL_CODES);
// Mirrors verification.RESEND_COOLDOWN_SECONDS.
const RESEND_SECONDS = 60;
const COUNTRY_OPTIONS = COUNTRY_DIAL_CODES.map(([iso, dial]) => ({ value: iso, label: `${iso} ${dial}` }));

const EMPTY_SIGNUP = {
  organization_name: "", first_name: "", last_name: "", country: "IN", phone: "", signup_source: "",
};

function Logo() {
  return (
    <div className="auth-logo">
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
  );
}

function GoogleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z" />
      <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z" />
    </svg>
  );
}

function EyeIcon({ off }: { off: boolean }) {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
      <path d="M1 8s2.5-4.5 7-4.5S15 8 15 8s-2.5 4.5-7 4.5S1 8 1 8z" />
      <circle cx="8" cy="8" r="2" />
      {off && <path d="M2.5 13.5l11-11" />}
    </svg>
  );
}

// Each role lands on a page it can use: supervisor and agent have no console,
// and /tenants is superadmin-only.
function landOn(router: ReturnType<typeof useRouter>, role: UserRole) {
  if (!isConsoleRole(role)) router.push("/no-access");
  else if (role === "superadmin") router.push("/tenants");
  else router.push("/dashboard");
}

// Sign-in and public signup on one screen. Signup always creates a new
// organization with the registrant as its admin (POST /auth/register).
export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"signin" | "create" | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [signup, setSignup] = useState(EMPTY_SIGNUP);
  const [reveal, setReveal] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verifyingEmail, setVerifyingEmail] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [resendIn, setResendIn] = useState(0);
  const [resetStep, setResetStep] = useState<"email" | "code" | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const setValue = (field: keyof typeof EMPTY_SIGNUP) => (value: string) =>
    setSignup((s) => ({ ...s, [field]: value }));
  const setField = (field: keyof typeof EMPTY_SIGNUP) =>
    (e: React.ChangeEvent<HTMLInputElement>) => setValue(field)(e.target.value);

  useEffect(() => {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const oauthToken = fragment.get("token");
    const oauthError = fragment.get("error");
    if (oauthToken || oauthError) window.history.replaceState(null, "", window.location.pathname);
    if (oauthToken) {
      setToken(oauthToken);
      getCurrentUser()
        .then((user) => landOn(router, user.role))
        .catch((e) => {
          clearToken();
          setError(e instanceof ApiError ? e.detail : String(e));
          setMode("signin");
        });
      return;
    }
    // Deferred so the state updates don't run synchronously inside the effect.
    queueMicrotask(() => {
      if (oauthError) setError(oauthError);
      setMode("signin");
    });
  }, [router]);

  useEffect(() => {
    if (resendIn <= 0) return;
    const t = setTimeout(() => setResendIn((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [resendIn]);

  const creating = mode === "create";

  const switchMode = (next: "signin" | "create") => {
    setMode(next);
    setError(null);
  };

  const startVerifying = (address: string, message: string) => {
    setVerifyingEmail(address);
    setCode("");
    setError(null);
    setNotice(message);
    setResendIn(RESEND_SECONDS);
  };

  const handleVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!verifyingEmail || code.length !== 6) {
      setError("Enter the 6-digit code from your email.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await verifyEmail(verifyingEmail, code);
      setToken(result.access_token);
      landOn(router, result.user.role);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      setCode("");
    } finally {
      setSubmitting(false);
    }
  };

  const handleResend = async () => {
    if (!verifyingEmail) return;
    setError(null);
    try {
      await resendVerificationCode(verifyingEmail);
      setNotice(`We sent a new code to ${verifyingEmail}.`);
      setResendIn(RESEND_SECONDS);
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        setResendIn(Number(e.detail.match(/(\d+) seconds/)?.[1]) || RESEND_SECONDS);
      }
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const openReset = (step: "email" | "code" | null) => {
    setResetStep(step);
    setError(null);
    setCode("");
    setNewPassword("");
  };

  // The server answers the same whether or not the email has an account.
  const sendResetCode = async () => {
    setError(null);
    try {
      await forgotPassword(email);
      setNotice(`If an account exists for ${email}, we sent it a 6-digit code.`);
      setResendIn(RESEND_SECONDS);
      return true;
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      return false;
    }
  };

  const handleForgot = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    if (await sendResetCode()) openReset("code");
    setSubmitting(false);
  };

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault();
    if (code.length !== 6) {
      setError("Enter the 6-digit code from your email.");
      return;
    }
    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await resetPassword(email, code, newPassword);
      setToken(result.access_token);
      landOn(router, result.user.role);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      setCode("");
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (creating && password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    const phoneDigits = signup.phone.replace(/\D/g, "");
    // Mirrors RegisterRequest.phone: 6–14 digits after the country code.
    if (creating && (phoneDigits.length < 6 || phoneDigits.length > 14)) {
      setError("Enter a phone number of 6 to 14 digits.");
      return;
    }
    if (creating && !signup.signup_source) {
      setError("Please tell us how you heard about us.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      if (creating) {
        const { country, ...profile } = signup;
        const result = await register({
          ...profile, email, password, phone: `${DIAL_CODE[country]} ${phoneDigits}`,
        });
        startVerifying(result.email, `We sent a 6-digit code to ${result.email}.`);
      } else {
        const result = await login(email, password);
        setToken(result.access_token);
        landOn(router, result.user.role);
      }
    } catch (e) {
      if (!creating && e instanceof ApiError && e.status === 403) {
        startVerifying(email.trim().toLowerCase(), e.detail);
      } else {
        setError(e instanceof ApiError ? e.detail : String(e));
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (mode === null) return null;

  const hero = HERO[mode];

  return (
    <div className="auth-split">
      <aside className="auth-hero">
        <Logo />
        <div className="auth-hero-main">
          <div className="auth-eyebrow">{hero.eyebrow}</div>
          <h1 className="auth-hero-title">{hero.title}</h1>
          <p className="auth-hero-body">{hero.body}</p>
          <div className="auth-providers">
            <span className="auth-providers-label">Works with</span>
            {PROVIDERS.map((p) => <span key={p} className="auth-chip">{p}</span>)}
          </div>
        </div>
        <div className="auth-facts">
          {hero.facts.map((f) => (
            <div key={f.label}>
              <div className="auth-fact-value">{f.value}</div>
              <div className="auth-fact-label">{f.label}</div>
            </div>
          ))}
        </div>
      </aside>

      <main className="auth-panel">
        <Link href="/" className="auth-back"><ArrowLeft size={13} /> Back to homepage</Link>
        <div className="auth-form">
          <div className="auth-mobile-logo"><Logo /></div>
          {verifyingEmail ? (
            <>
              <h2 className="auth-title">Check your email</h2>
              <p className="auth-sub">{notice}</p>
              {error && <div className="error-banner">{error}</div>}
              <form onSubmit={handleVerify}>
                <CodeInput value={code} onChange={setCode} disabled={submitting} />
                <button
                  className="login-btn auth-submit"
                  type="submit"
                  disabled={submitting || code.length !== 6}
                >
                  {submitting ? "Verifying…" : "Verify email"}
                </button>
              </form>
              <div className="login-alt">
                Didn&apos;t get it?{" "}
                <button type="button" onClick={handleResend} disabled={resendIn > 0}>
                  {resendIn > 0 ? `Resend in ${resendIn}s` : "Resend code"}
                </button>
              </div>
              <div className="login-alt">
                <button type="button" onClick={() => { setVerifyingEmail(null); setError(null); }}>
                  <ArrowLeft size={13} /> Use a different email
                </button>
              </div>
            </>
          ) : resetStep === "email" ? (
            <>
              <h2 className="auth-title">Reset your password</h2>
              <p className="auth-sub">Enter your account email and we&apos;ll send you a 6-digit code.</p>
              {error && <div className="error-banner">{error}</div>}
              <form onSubmit={handleForgot}>
                <div className="login-field">
                  <label className="login-label" htmlFor="reset-email">Email</label>
                  <input
                    id="reset-email"
                    className="login-input"
                    type="email"
                    autoComplete="email"
                    placeholder="name@company.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                  />
                </div>
                <button className="login-btn auth-submit" type="submit" disabled={submitting}>
                  {submitting ? "Sending…" : "Send code"}
                </button>
              </form>
              <div className="login-alt">
                <button type="button" onClick={() => openReset(null)}><ArrowLeft size={13} /> Back to sign in</button>
              </div>
            </>
          ) : resetStep === "code" ? (
            <>
              <h2 className="auth-title">Choose a new password</h2>
              <p className="auth-sub">{notice}</p>
              {error && <div className="error-banner">{error}</div>}
              <form onSubmit={handleReset}>
                <CodeInput value={code} onChange={setCode} disabled={submitting} />
                <div className="login-field">
                  <label className="login-label" htmlFor="reset-password">New password</label>
                  <div className="login-input-wrap">
                    <input
                      id="reset-password"
                      className="login-input"
                      type={reveal ? "text" : "password"}
                      autoComplete="new-password"
                      placeholder="At least 8 characters"
                      minLength={8}
                      value={newPassword}
                      onChange={(e) => setNewPassword(e.target.value)}
                      required
                    />
                    <button
                      type="button"
                      className="login-reveal"
                      onClick={() => setReveal((r) => !r)}
                      aria-label={reveal ? "Hide password" : "Show password"}
                      aria-pressed={reveal}
                      title={reveal ? "Hide password" : "Show password"}
                    >
                      <EyeIcon off={reveal} />
                    </button>
                  </div>
                </div>
                <button
                  className="login-btn auth-submit"
                  type="submit"
                  disabled={submitting || code.length !== 6}
                >
                  {submitting ? "Resetting…" : "Reset password"}
                </button>
              </form>
              <div className="login-alt">
                Didn&apos;t get it?{" "}
                <button type="button" onClick={sendResetCode} disabled={resendIn > 0}>
                  {resendIn > 0 ? `Resend in ${resendIn}s` : "Resend code"}
                </button>
              </div>
              <div className="login-alt">
                <button type="button" onClick={() => openReset("email")}><ArrowLeft size={13} /> Use a different email</button>
              </div>
            </>
          ) : (
            <>
              <h2 className="auth-title">{creating ? "Create an account" : "Sign in"}</h2>
              <p className="auth-sub">
                {creating
                  ? "Enter your details to get started. You'll be the admin of your new organization."
                  : "Welcome back. Enter your credentials to continue."}
              </p>
              {error && <div className="error-banner">{error}</div>}
              <a className="login-google" href={googleSignInUrl(creating ? "create" : "signin")}>
                <GoogleIcon />
                {creating ? "Sign up with Google" : "Continue with Google"}
              </a>
              <div className="login-divider">or</div>
              <form onSubmit={handleSubmit}>
                {creating && (
                  <>
                    <div className="login-field">
                      <label className="login-label" htmlFor="signup-org">Organization Name</label>
                      <input
                        id="signup-org"
                        className="login-input"
                        autoComplete="organization"
                        placeholder="Acme Corp"
                        maxLength={120}
                        value={signup.organization_name}
                        onChange={setField("organization_name")}
                        required
                      />
                    </div>
                    <div className="login-row">
                      <div className="login-field">
                        <label className="login-label" htmlFor="signup-first">First Name</label>
                        <input
                          id="signup-first"
                          className="login-input"
                          autoComplete="given-name"
                          placeholder="John"
                          maxLength={80}
                          value={signup.first_name}
                          onChange={setField("first_name")}
                          required
                        />
                      </div>
                      <div className="login-field">
                        <label className="login-label" htmlFor="signup-last">Last Name</label>
                        <input
                          id="signup-last"
                          className="login-input"
                          autoComplete="family-name"
                          placeholder="Doe"
                          maxLength={80}
                          value={signup.last_name}
                          onChange={setField("last_name")}
                          required
                        />
                      </div>
                    </div>
                  </>
                )}
                <div className="login-field">
                  <label className="login-label" htmlFor="login-email">Email</label>
                  <input
                    id="login-email"
                    className="login-input"
                    type="email"
                    autoComplete="email"
                    placeholder="name@company.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                  />
                </div>
                {creating && (
                  <div className="login-field">
                    <label className="login-label" htmlFor="signup-phone">Phone Number</label>
                    <div className="login-phone">
                      <SelectMenu
                        ariaLabel="Country code"
                        value={signup.country}
                        options={COUNTRY_OPTIONS}
                        onChange={setValue("country")}
                      />
                      <input
                        id="signup-phone"
                        className="login-input"
                        type="tel"
                        autoComplete="tel-national"
                        placeholder="9876543210"
                        pattern="[0-9 ]{6,18}"
                        value={signup.phone}
                        onChange={setField("phone")}
                        required
                      />
                    </div>
                  </div>
                )}
                <div className="login-field">
                  <label className="login-label" htmlFor="login-password">Password</label>
                  <div className="login-input-wrap">
                    <input
                      id="login-password"
                      className="login-input"
                      type={reveal ? "text" : "password"}
                      autoComplete={creating ? "new-password" : "current-password"}
                      placeholder={creating ? "At least 8 characters" : "••••••••"}
                      minLength={creating ? 8 : undefined}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                    />
                    <button
                      type="button"
                      className="login-reveal"
                      onClick={() => setReveal((r) => !r)}
                      aria-label={reveal ? "Hide password" : "Show password"}
                      aria-pressed={reveal}
                      title={reveal ? "Hide password" : "Show password"}
                    >
                      <EyeIcon off={reveal} />
                    </button>
                  </div>
                </div>
                {creating && (
                  <div className="login-field">
                    <label className="login-label" htmlFor="signup-source">How did you hear about us?</label>
                    <SelectMenu
                      id="signup-source"
                      value={signup.signup_source}
                      options={SIGNUP_SOURCES}
                      placeholder="Select an option"
                      onChange={setValue("signup_source")}
                    />
                  </div>
                )}
                <button className="login-btn auth-submit" type="submit" disabled={submitting}>
                  {submitting
                    ? creating ? "Creating account…" : "Signing in…"
                    : creating ? "Create account" : "Sign in"}
                </button>
              </form>
              {!creating && (
                <div className="login-alt">
                  <button type="button" onClick={() => openReset("email")}>Forgot password?</button>
                </div>
              )}
              <div className="login-alt">
                {creating ? (
                  <>
                    Already have an account?{" "}
                    <button type="button" onClick={() => switchMode("signin")}>Sign in</button>
                  </>
                ) : (
                  <>
                    New here?{" "}
                    <button type="button" onClick={() => switchMode("create")}>Create an account</button>
                  </>
                )}
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
