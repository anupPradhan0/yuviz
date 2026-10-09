import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Phone menus (IVR) moved off /workflows, which now only hosts agents' conversation steps.
  async redirects() {
    return [
      { source: "/workflows", destination: "/phone-menus", permanent: true },
      { source: "/workflows/new", destination: "/phone-menus/new", permanent: true },
      { source: "/workflows/flows/:id", destination: "/phone-menus/:id", permanent: true },
    ];
  },
};

export default nextConfig;
