"use client";

// The optional "actions" part of the Easy business step: tick the account's
// existing custom APIs. The most relevant ones for the chosen job come first.
// All text comes from lib/easyCopy.

import Link from "next/link";
import { easyCopy } from "@/lib/easyCopy";
import type { CustomApi } from "@/lib/toolexecApi";

const ACTIONS_HREF = "/knowledge-bases";

const JOB_KEYWORDS: Record<string, string[]> = {
  "appointment-booking": ["slot", "book", "availab", "calendar", "appointment", "schedul"],
  "payment-reminder": ["pay", "invoice", "balance", "due"],
  "renewal-offer": ["renew", "plan", "subscription"],
  "csat-survey": ["survey", "feedback", "rating"],
  "inbound-triage": ["ticket", "status", "lookup"],
};

// Stable: matches first, everything else after in its original order.
export function sortByRelevance(apis: CustomApi[], jobId: string | null): CustomApi[] {
  const keywords = (jobId && JOB_KEYWORDS[jobId]) || [];
  const matches = (api: CustomApi) => {
    const text = `${api.name} ${api.description}`.toLowerCase();
    return keywords.some((k) => text.includes(k));
  };
  return [...apis.filter(matches), ...apis.filter((a) => !matches(a))];
}

export function EasyToolsPicker({
  apis,
  jobId,
  ticked,
  onToggle,
}: {
  apis: CustomApi[];
  jobId: string | null;
  ticked: string[];
  onToggle: (id: string) => void;
}) {
  return (
    <div className="form-group" style={{ marginTop: 14, marginBottom: 0 }}>
      <div className="form-label">{easyCopy.actionsLabel}</div>
      <div className="form-hint">{easyCopy.actionsHint}</div>
      {apis.length > 0 ? (
        <fieldset style={{ border: 0, padding: 0, margin: "8px 0 0" }}>
          {sortByRelevance(apis, jobId).map((api) => (
            <label key={api.id} style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={ticked.includes(api.id)} onChange={() => onToggle(api.id)} />
              <span>
                {api.description || api.name}
                {api.description && <span className="form-hint"> ({api.name})</span>}
              </span>
            </label>
          ))}
        </fieldset>
      ) : (
        <div className="form-hint" style={{ marginTop: 8 }}>
          {easyCopy.actionsNone} <Link href={ACTIONS_HREF}>{easyCopy.actionsNoneLink}</Link>
        </div>
      )}
    </div>
  );
}
