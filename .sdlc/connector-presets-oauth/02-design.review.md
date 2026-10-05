# Design review: connector-presets-oauth (after round-3 security revise)

Recorded by the coordinator from the critic's returned report; the critic did not write this file itself.
Critic's own caveats: it did not verify that every Config consumer authenticates as a platform
service account, and it did not check for other constructors of PipelineConversationHandler.

1. [blocking] A body param that uses `body_path` is no longer redacted by name.
   - `redaction.redact` indexes a flat dict by bare name (redaction.py:11-13, 33). `caller_phone` is
     nested at `extendedProperties.private.yuviz_phone`, so redacting by that name does nothing.
   - The phone number lands in `api_chain_steps.arguments_redacted`, which every role can read via
     GET /calls/{session_id}/chain-runs.
   - Test 14's assertion cannot pass as written. The gate's `arguments_redacted` (Changes (f)) has the
     same gap, and it is referenced at executor.py:~830 before it is computed at :879.
   - Fix: derive each sensitive param's redaction path from its `body_path`, build
     `arguments_redacted` before the gate, and have test 14 assert on the nested path.
2. [minor] `auth_schemes.apply()` (executor.py:808) runs before the confirmation gate (:833+).
   - The first, unconfirmed call can trigger a token refresh or flip the connection to
     `reconnect_needed`.
   - Fix: move the gate before `apply()`, or reword the claim as "no resource-API request" and run
     test 7 with an expired token.
3. [minor] Scope: the Config credential work is separable.
   - It is roughly 10 of the 34 rows: provider_configs, telephony_configs and tool_provider_configs,
     their routers, SecretRefInput.tsx, the telephony page and test 15. No PRD acceptance criterion
     needs it.
   - Keep only the read-only Config evidence scan in reencrypt_tenant_refs.py. Move the rest to its
     own change with its own security review.
4. [minor] Stale line cite: `called_did=call.to_number` is at orchestrator.py:200, not :198.

VERDICT: AMBER
