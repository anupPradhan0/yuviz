import type { DocumentStatus } from "./knowledgeApi";

/** Replace a folder's files at its existing position during background polling. */
export function mergeKnowledgeFolderRows<Row extends { kb_id: string }>(
  current: readonly Row[],
  updates: ReadonlyMap<string, readonly Row[]>,
): Row[] {
  const next: Row[] = [];
  const replaced = new Set<string>();
  for (const row of current) {
    const updated = updates.get(row.kb_id);
    if (!updated) {
      next.push(row);
    } else if (!replaced.has(row.kb_id)) {
      next.push(...updated);
      replaced.add(row.kb_id);
    }
  }
  return next;
}

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
