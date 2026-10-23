import type { DocumentStatus } from "./knowledgeApi";

export const FILE_STATUS: Record<
  DocumentStatus,
  { label: string; color: string }
> = {
  ready: { label: "Ready", color: "green" },
  ready_degraded: { label: "Needs a check", color: "amber" },
  pending: { label: "Preparing…", color: "amber" },
  processing: { label: "Preparing…", color: "amber" },
  failed: { label: "Needs a fix", color: "red" },
};
