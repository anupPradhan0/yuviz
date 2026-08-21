# Sales Agent — Talking Scripts

Reference call scripts for the `sales` agent under the `yuviz` tenant
(DID 5009). These reflect the agent's actual current behavior — validated
by testing the live `system_prompt` directly against the live LLM
(`qwen2.5:7b` via Ollama) before writing these down, not written from
imagination. See `agent.system_prompt` (Config Service,
`GET /tenants/yuviz/agents/sales`) for the source of truth; if that prompt
changes, these scripts should be re-validated the same way rather than
assumed to still match.

Turns are short and single-idea on purpose — that's an explicit guardrail
in the prompt (`# Guardrails` → "1-2 sentences, ONE idea, then stop"),
added after early testing showed the model would otherwise chain a
question, a pitch, and a close into one long reply.

## Script 1 — Cold Open, Industry Unknown → Real Estate

> **Sales:** Hi, this is Sales calling from Yuviz.ai. We help businesses like yours stop losing leads to slow follow-up. Do you have a quick minute?
> **Prospect:** Sure, go ahead.
> **Sales:** Great — what kind of business are you running?
> **Prospect:** I run a small real estate brokerage.
> **Sales:** Got it. In real estate, a missed lead usually means a lost commission — how are you handling inquiries after hours right now?
> **Prospect:** Honestly, my agents can't be online 24/7, so a lot of leads just go cold overnight.
> **Sales:** That's exactly the gap we close — our AI agents engage a new lead instantly, day or night, and book it straight onto your calendar. Want to see a quick demo of that?
> **Prospect:** Yeah, I'd like to see that.
> **Sales:** Perfect — would tomorrow at 2 PM work for a short demo?
> **Prospect:** Tomorrow at 2 works.
> **Sales:** I've booked that in, you'll get a confirmation shortly. Talk soon!
> *(→ `end_call`)*

## Script 2 — Healthcare, Objection → Live Transfer

> **Sales:** Hi, this is Sales from Yuviz.ai. We help healthcare providers automate patient inquiries and scheduling. Do you have a minute?
> **Prospect:** I'm not sure about using AI for patients, honestly. Sounds risky.
> **Sales:** That's a fair concern — accuracy matters most here. Our agents strictly follow your protocols; they just handle the repetitive stuff, like hours or insurance questions. Does your staff spend a lot of time on those?
> **Prospect:** They do, it's a real drain.
> **Sales:** I'd love to have one of our specialists walk you through the accuracy and security side directly — want me to transfer you now?
> **Prospect:** Actually, yes, that would help.
> **Sales:** Transferring you now, one moment.
> *(→ live transfer initiated)*

## Script 3 — Insurance, Initial Rejection → Recovered

> **Sales:** Hi, this is Sales from Yuviz.ai. We help insurance agencies capture and qualify leads instantly. Interested in hearing how we cut response time to zero?
> **Prospect:** No thanks, we already have a system.
> **Sales:** Understood — quick question though: does your current system engage a new lead within the first 30 seconds, any time of day?
> **Prospect:** No, it usually takes a few hours.
> **Sales:** That gap is where most agencies lose leads without realizing it. Worth a 10-minute call to see how we close it?
> **Prospect:** I suppose it wouldn't hurt.
> **Sales:** I'll book a quick discovery call — does Thursday morning work?
> **Prospect:** Thursday morning's fine.
> **Sales:** Great, I'll send the confirmation now. Talk then!
> *(→ `end_call`)*

## Script 4 — Cost Objection (any vertical)

> **Prospect:** This sounds expensive, we don't really have budget for AI right now.
> **Sales:** I hear that a lot — think of it less as a cost and more as replacing an expensive manual process with one that runs 24/7 without extra headcount. Want a quick look at how that pencils out for your business?

## Known defects these scripts were written to avoid

Found by testing the prompt directly against the live model before these
guardrails existed — kept here so a future prompt edit doesn't
reintroduce them silently:

- **Literal placeholder text spoken aloud** — the model would say things
  like "Would `[Time]` work?" or address "`[Prospect]`" by name, copying
  template-style bracket syntax from its own training data even though
  the prompt never used brackets. Guarded against explicitly in
  `# Guardrails`.
- **No fallback for "industry unknown"** — without an explicit
  instruction, the model either guessed all three verticals at once or
  left a placeholder for later instead of just asking. Guarded against in
  `# Goal`.
- **Writing out the whole call as one turn** — with stage directions like
  `(If the prospect shows interest)` and imagined prospect replies,
  instead of responding to only the current turn. Guarded against
  explicitly in `# Guardrails`.
