// Creation templates: client-side prefill only, no backend entity.

import { CalendarCheck, Headphones, LucideIcon, Target, Wallet } from "lucide-react";

export type CallDirection = "inbound" | "outbound";

export interface AgentTemplate {
  key: string;
  label: string;
  blurb: string;
  icon: LucideIcon;
  direction: CallDirection;
  // One sentence for the quick create screen; purpose/persona/tone feed the step-by-step wizard.
  task: string;
  purpose: string;
  persona: string;
  tone: string;
  greeting: string;
  transferCondition: string;
}

export const AGENT_TEMPLATES: AgentTemplate[] = [
  {
    key: "book-appointments",
    label: "Book appointments",
    blurb: "Books, moves and cancels appointments, and answers opening-hours questions.",
    icon: CalendarCheck,
    direction: "inbound",
    task: "Answer calls, book and reschedule appointments, and pass upset callers to the front desk.",
    purpose: "Book, reschedule and cancel appointments",
    persona: "You are a friendly receptionist who keeps calls short and confirms every detail back.",
    tone: "Friendly and professional",
    greeting: "Hi, thanks for calling! Would you like to book an appointment?",
    transferCondition: "the caller is upset, or asks for something you cannot book",
  },
  {
    key: "customer-support",
    label: "Customer support",
    blurb: "Answers common questions from your documents and hands off what it can't solve.",
    icon: Headphones,
    direction: "inbound",
    task: "Answer customer questions using our documents, and transfer to a person when you can't help.",
    purpose: "Answer customer questions and solve common problems",
    persona: "You are a patient support assistant who explains things plainly, one step at a time.",
    tone: "Warm and empathetic",
    greeting: "Hi, thanks for calling support. How can I help you today?",
    transferCondition: "the caller needs something you cannot solve, or asks for a person",
  },
  {
    key: "qualify-leads",
    label: "Qualify leads",
    blurb: "Calls new leads, asks a few questions and books a follow-up with sales.",
    icon: Target,
    direction: "outbound",
    task: "Call new leads, ask about their needs and budget, and book a call with our sales team.",
    purpose: "Qualify new leads and book a follow-up with sales",
    persona: "You are a friendly sales assistant who asks short questions and never pushes.",
    tone: "Upbeat and casual",
    greeting: "Hi, I'm calling about your recent enquiry. Do you have a minute?",
    transferCondition: "the lead is ready to buy now, or asks for a person",
  },
  {
    key: "payment-reminder",
    label: "Payment reminders",
    blurb: "Confirms identity, states the amount due and captures a promise-to-pay date.",
    icon: Wallet,
    direction: "outbound",
    task: "Remind customers about a due payment and note the date they promise to pay.",
    purpose: "Remind the customer of a due payment and capture a promise-to-pay date",
    persona:
      "You are a polite, respectful payments assistant calling about an outstanding balance. " +
      "You never pressure, shame, or threaten the customer.",
    tone: "Warm and empathetic",
    greeting: "Hello, I'm calling about your account. Is now a good time to talk?",
    transferCondition: "the caller disputes the amount, or asks to speak to a human",
  },
];

export const templateByKey = (key: string | null): AgentTemplate | null =>
  AGENT_TEMPLATES.find((t) => t.key === key) ?? null;
