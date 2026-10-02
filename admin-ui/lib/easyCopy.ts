// Every user-visible string in the Easy agent-creation flow. Easy renders text
// from here and never a server `detail`. Keep these plain-spoken: the Python
// test scans every string literal in this file for banned technical words.

import { ApiError } from "./api";

export const easyCopy = {
  stepTitles: [
    "What should my AI receptionist do?",
    "Tell it about my business",
    "Choose how it speaks",
    "Test it",
    "Fix anything that's wrong",
    "Put it to work",
  ],

  // A job needs something the account has none of: show this and link to where it is added.
  connectAiService: "Connect an AI service to continue",
  setUpSpeechRecognition: "Set up speech recognition to continue",
  setUpVoice: "Set up a voice to continue",

  // Step 3 dropdowns appear only when the account has more than one choice.
  voiceLabel: "Voice",
  aiServiceLabel: "AI service",
  speechRecognitionLabel: "Speech recognition",

  doubleBraces: "Please remove double curly brackets {{ }} from this text.",

  notFixable:
    "These instructions were changed by hand, so they can't be fixed automatically here. You can still edit them on the receptionist's page.",
  customerData:
    "That fix would copy details from a specific customer into the instructions. Describe the problem in general terms and try again.",
  customerDataExample: "For example: it asked for the caller's date of birth before checking who they were.",
  unusableOutput: "We couldn't turn that into a safe fix. Try describing the problem differently.",
  aiUnavailable: "The AI service didn't answer. Nothing was changed. Please try again.",
  aiCantRunThis: "Your AI service can't run this yet.",
  tooManyRequests: "You're going a little fast. Please wait a moment and try again.",
  testExpired: "This test has ended. Start a new one.",
  changedMeanwhile: "Something changed while you were working. Reload the page and try again.",
  notAllowed: "You don't have permission to do that.",
  notFound: "We couldn't find that. Reload the page and try again.",
  generic: "Something went wrong. Please try again.",
} as const;

export function easyErrorText(err: unknown): string {
  if (!(err instanceof ApiError)) return easyCopy.generic;
  const { status, detail } = err;
  if (detail === "customer_data") return easyCopy.customerData;
  if (detail === "unusable_output") return easyCopy.unusableOutput;
  if (detail === "prompt_not_fixable") return easyCopy.notFixable;
  if (detail === "ai_unavailable" || status === 502) return easyCopy.aiUnavailable;
  if (detail.includes("double curly brackets")) return easyCopy.doubleBraces;
  if (status === 429) return easyCopy.tooManyRequests;
  if (status === 404 && detail === "test session not found") return easyCopy.testExpired;
  if (status === 404) return easyCopy.notFound;
  if (status === 409) return easyCopy.changedMeanwhile;
  if (status === 403) return easyCopy.notAllowed;
  return easyCopy.generic;
}
