# Code review

VERDICT: GREEN

Scope: `git diff 4918800..HEAD` (six commits). Reviewed for correctness, phase seams,
design-claim drift and over-engineering. The load-bearing security mechanisms were traced
individually (see "Mechanisms verified" below); none is missing or mis-implemented.

1. [minor] `direction` default flipped from `"inbound"` to `""` on a handler whose
   `self._direction` also feeds the transcript writer —
   `services/conversation/pipeline.py:452` (default), consumed at `:683`
   (`transcripts.begin_call(..., self._direction, ...)`) — fails when: any
   `PipelineConversationHandler` constructed without `direction=` while a `TranscriptBuilder`
   with a live pool is attached. `begin_call` now carries `""` into the `calls` insert, and
   `calls_direction_check CHECK (direction IN ('inbound','outbound','test'))`
   (`database/schema.sql:597`) rejects it, so the whole call row (and the transcript hung off
   it) is lost instead of being recorded as inbound. The toolexec fail-closed intent (empty
   direction -> `caller_id_unavailable`) is correct and worth keeping; the defect is that one
   field now serves two sinks with different validity rules (lesson 33 shape). Production is
   not currently exposed — `services/conversation/__main__.py:370` always passes
   `direction=ctx.direction`, and telephony sets it at `services/telephony/app.py:99` and
   `orchestrator.py:202,216` — fix: keep the `""` default for the tool path but normalize at
   the transcript call site (`self._direction or "inbound"` at `pipeline.py:683`), or pass the
   direction to toolexec as its own attribute.

2. [minor] The Sheets setup call runs outside the advisory lock and outside the transaction —
   `services/toolexec/presets.py:653-657` (`_create_lead_sheet`), lock taken only at `:674` —
   fails when: two `apply_preset("sheets_lead_capture")` calls for one tenant overlap, or one
   is retried after a later failure in the same function. Both see `sheets_capture_lead`
   absent in the pre-lock read at `:646-650`, both create a Google spreadsheet, and only one
   id is ever written to a row — contradicting the comment at `:654` ("re-applying must not
   leave a spreadsheet nobody uses") and the docstring's "a failure leaves no rows" (true for
   rows, not for this external side effect). Fix: do the existence read inside the locked
   transaction and create the sheet only after the lock is held, or state the orphan as
   accepted in the runbook.

## Mechanisms verified (no finding)

- `allow_pointer_schemes` is required with no default on the helper and on all five call
  sites including `scripts/seed_default_config.py:103,113` (literal `False`, with the
  "never add a default" comment the tasks asked for).
- `resolve_api_key_input(..., current_ref=old[...])` sits behind `SELECT … FOR UPDATE` on
  the Config update paths; `tests/test_cross_tenant_admin.py` exercises the keyword.
- Quarantine script: conditional `UPDATE … WHERE id = $1 AND <col> = $2`, abort on a status
  other than `UPDATE 1` (`reencrypt_tenant_refs.py:238,252,260,271`), blast-radius ceiling
  checked before any write (`:365`), `--dry-run` rolls back, exit 2 on ceiling.
- `caller_id`: resolved only from `remote_party` (`executor.py:459-464`), `caller_arguments`
  is never read for it; `_remote_party` (`:93-109`) refuses the tenant's own DID and
  `presets.remote_party_number` (`presets.py:332-346`) returns None for any direction other
  than inbound/outbound or two legs that normalize equal. The model's tool schema is built
  from `p.source = 'caller'` only (`policy_resolver.py:261`), so a `caller_id` param is never
  offered. Tests at `test_preset_execution.py:297-400` feed the model's own contradicting
  number and assert it never reaches the wire — a test that can fail.
- Send cap: `_sends_in_session` carries `r.tenant_id = $1` (`executor.py:718-725`), and a
  missing `session_id` fails closed (`:997-1003`). The cap tests vary the body per send, so
  the side-effect claim is not the thing doing the blocking.
- OAuth state: single conditional `UPDATE … SET consumed_at = now() WHERE tenant_id AND
  state_hash AND user_id AND consumed_at IS NULL AND expires_at > now() RETURNING …`
  (`oauth.py:214-220`), exchange strictly after the redeem commits; scopes come only from
  `presets.PRESETS` with a `preset.provider != provider` check (`:169-172`) — the duplicated
  `_PRESET_SCOPES` map is gone.
- `disconnect` (`oauth.py:406-438`): constant `{"disconnected": True}` body, foreground work
  is one conditional write plus the audit row, and the cross-tenant shared-grant probe plus
  the revoke run in `background_tasks`, so neither body nor latency varies with another
  tenant's state.
- Connector token binding: `auth_schemes.apply` checks the endpoint host against
  `provider.api_hosts` before setting `Authorization` (`auth_schemes.py:228-234`), same check
  in `oauth.post_json` (`:312`).
- `services/telephony/accounts.py:134-145` now returns None on a quarantined ref instead of
  passing it through — the false design claim is closed.
