Issue https://github.com/yuviz-ai/yuviz/issues/128
# LLM can end a live call with [[END_CALL]] mid-conversation — guard it

## Problem

The LLM can hang up on a caller mid-conversation. The pipeline ends the call whenever the model's reply contains `[[END_CALL]]` (`services/conversation/directives.py:133` → `pipeline.py:1354`). Nothing checks that the conversation is actually over, so one bad generation drops a live caller.

## Seen in a local web test (2026-10-18)

The test used the default agent: Ollama `llama3.2` and faster-whisper `small` (CPU), with `supported=en,hi`.

| Turn | Caller | Agent |
|---|---|---|
| 1 | (Hindi) | Replied in Hindi |
| 2 | "Okay, what else can you do for me?" | Replied in Hindi with a question asking for more detail, then `[[END_CALL]]` |

The caller asked an open question, the agent asked one back, and then the call was ended. None of the end conditions in `_END_CALL_CONDITION_DEFAULT` (`pipeline.py:105`) applied: no goodbye, no "no more questions", nothing resolved. Smaller local models are the most likely to do this, but any model can.

## Proposed guard

Before honouring `EndCallDirective`, require at least one of these:

1. **The caller signalled closure** in their last utterance, such as a goodbye or "that's all" / "no more questions". This must work in every supported language, so it can't be an English keyword list. It could use the i18n tables or a cheap classifier.
2. **The reply reads as a farewell.** For example, a reply ending in a question to the caller (`?`, or `।` / `؟` and similar) should never end the call.
3. **The agent is in scripted mode** (`_build_end_call_instruction(scripted=True)`), and the reply is only the token.

When the guard rejects a marker:
- strip the token,
- speak the reply as usual,
- keep the call open,
- log `end_call_rejected reason=<...> session=<id>` for tuning.

## Acceptance criteria

- [ ] A reply that ends in a question to the caller, followed by `[[END_CALL]]`, does not end the call. The text is still spoken.
- [ ] Caller says goodbye, then the agent's farewell plus `[[END_CALL]]`: the call ends as it does today.
- [ ] Scripted mode with the token as the only reply: the call ends as it does today.
- [ ] The closing-intent check works for every language discovered in `services/conversation/i18n/`, not just English.
- [ ] Each rejected marker is logged once, with a reason and no transcript text.
- [ ] No added latency on the normal turn path. The check runs only when a marker is present.

## Out of scope

- Mid-call language drift (the model replying in Hindi after the session switched to English). That's a model-quality issue; PR #103's safety net already voices those sentences in the language they're written in.

