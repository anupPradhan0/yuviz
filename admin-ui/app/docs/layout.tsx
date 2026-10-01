import "@/app/globals.css";
import Link from "next/link";

// /docs lives outside app/(console)/ on purpose: it must render when the
// console (auth, API, AppShell) is what's broken. That also means nothing
// above it imports the stylesheet or offers a way back, so both are here.
// Theme follows the OS preference (globals.css), since AppShell, which
// sets data-theme, is not mounted on this route.
export default function DocsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div style={{ minHeight: "100vh", background: "var(--bg)", color: "var(--text)" }}>
      <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--border)" }}>
        <Link href="/dashboard" className="btn btn-ghost btn-sm">
          ← Console
        </Link>
      </div>
      {children}
    </div>
  );
}
