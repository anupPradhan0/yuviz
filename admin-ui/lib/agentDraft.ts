// Browser-only autosave for the /agents/new wizard, so leaving the page never loses typed work.

const DRAFT_KEY = "yuviz:new-agent-draft";

export interface AgentDraft {
  savedAt: number;
  step: string;
  name: string;
  tenantSlug: string;
  purpose: string;
  persona: string;
  tone: string;
  languageChoice: string;
  customLanguage: string;
  sttId: string | null;
  llmId: string | null;
  ttsId: string | null;
  maxCallDuration: number | "";
  goodbyeGraceMs: number | "";
  escalationThreshold: number | "";
  transferType: "none" | "cold" | "warm" | undefined;
  transferDestination: string;
  transferCondition: string;
  transferAnnouncement: string;
  complianceInstructions: string;
  fallbackResponse: string;
  selectedKbIds: string[];
  selectedApiIds: string[];
  greeting: string;
  systemPrompt: string;
  promptEdited: boolean;
}

export function loadAgentDraft(): AgentDraft | null {
  try {
    const raw = window.localStorage.getItem(DRAFT_KEY);
    return raw ? (JSON.parse(raw) as AgentDraft) : null;
  } catch {
    return null;
  }
}

export function saveAgentDraft(draft: AgentDraft): void {
  try {
    window.localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // Blocked/full storage: autosave silently degrades to none.
  }
}

export function clearAgentDraft(): void {
  try {
    window.localStorage.removeItem(DRAFT_KEY);
  } catch {
    // Same non-fatal fallback as saveAgentDraft.
  }
}

export function draftSavedLabel(savedAt: number): string {
  const mins = Math.floor((Date.now() - savedAt) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return new Date(savedAt).toLocaleDateString();
}
