import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // IVR call menus moved off /workflows (now agents' conversation steps).
  async redirects() {
    return [
      { source: "/workflows", destination: "/ivr-call", permanent: true },
      { source: "/workflows/new", destination: "/ivr-call/new", permanent: true },
      // UUID-only: /workflows/<tenantSlug>/<agentSlug> is conversation steps, and a tenant may be slugged "flows".
      { source: "/workflows/flows/:id([0-9a-fA-F-]{36})", destination: "/ivr-call/:id", permanent: true },
    ];
  },
};

export default nextConfig;
