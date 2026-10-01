"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getCurrentUser } from "@/lib/api";
import { TeamMembers } from "@/components/TeamMembers";

// Superadmin's cross-account view; everyone else manages their team in Settings.
export default function UsersPage() {
  const router = useRouter();
  const [isSuperadmin, setIsSuperadmin] = useState<boolean | null>(null);

  useEffect(() => {
    getCurrentUser()
      .then((me) => {
        if (me.role === "superadmin") setIsSuperadmin(true);
        else router.replace("/settings?section=team");
      })
      .catch(() => router.replace("/settings?section=team"));
  }, [router]);

  return isSuperadmin ? <TeamMembers /> : null;
}
