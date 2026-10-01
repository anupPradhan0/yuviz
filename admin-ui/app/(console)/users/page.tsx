"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, getCurrentUser } from "@/lib/api";
import { TeamMembers } from "@/components/TeamMembers";

// Superadmin's cross-account view; everyone else manages their team in Settings.
export default function UsersPage() {
  const router = useRouter();
  const [isSuperadmin, setIsSuperadmin] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getCurrentUser()
      .then((me) => {
        if (me.role === "superadmin") setIsSuperadmin(true);
        else router.replace("/settings?section=team");
      })
      // Only a known non-superadmin is redirected; a failed lookup says so.
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
  }, [router]);

  if (error) return <div className="error-banner">Couldn&apos;t load your account, so Users can&apos;t be shown: {error}</div>;
  return isSuperadmin ? <TeamMembers /> : null;
}
