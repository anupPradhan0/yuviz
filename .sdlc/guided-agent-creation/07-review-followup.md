# Code review: follow-up commits 5faa679..7eeea2c (PR #63)
VERDICT: RED

1. [blocking] The revise meta-prompt forces voice framing onto chat agents. `_META_RULES` is shared by generate (voice only) and `_REVISE_SYSTEM`, which is used for both channels. It requires the prompt to start with "...You are the AI receptionist for <the business>, on a live phone call." and to use the heading "Ending the call". — `services/config/system_prompt.py:184` (used at `:353`/`:380`) — fails when: an owner clicks Fix on a `faq-support` (chat) agent. `render()` wrote "on a live text chat", and the revise system prompt now tells the model to rewrite that line to "on a live phone call". `enforce_prompt_structure` and the accept path only check headings and blocks, so the wrong medium gets through and a chat agent is told it is on a phone call. — fix: build the role line and medium wording from `channel` (e.g. `_meta_rules(channel)`), the way `render()` already picks "phone call"/"text chat".

2. [minor] Agents created at template v1/v2 can no longer be revised. Their stored prompts have the old three headings (How you speak / Guardrails / Doing your job well). `check_prompt_structure` now requires the eight new headings. — `services/config/routers/agents.py:278`, `services/config/agents.py:88` — fails when: any agent created from this PR's earlier commits (dev or staging DBs; main never had the structure, so production is not affected) opens Fix. `prompt_fixable` is false and revise returns 422 `prompt_not_fixable`, so the agent shows "changed by hand" even though nobody edited it. — fix: accept the legacy three-heading shape in the pre-revise gate, or record in approvals/runbook that v1/v2 agents must be recreated.

3. [minor] `enableExecuteApi` is not idempotent, so after an ambiguous failure the retry can never clear. It always POSTs a new policy. — `admin-ui/lib/api.ts:1059` — fails when: `createAgentToolPolicy` commits on the server but the response is lost (timeout or proxy 5xx). Easy keeps `switchOn: true`, and every "Try again" then POSTs again and gets a conflict, so the actions warning never goes away even though execute_api is already on. In the Advanced wizard (`agents/new/page.tsx:243`), any failure here leaves the created agent behind with an error banner, and resubmitting creates a second agent. — fix: list the agent's tool policies first, and skip (or PATCH `enabled: true`) when an `execute_api` policy already exists.

4. [minor] Easy moves to the Test step before documents and actions are attached. — `admin-ui/components/EasyAgentFlow.tsx:300` — fails when: an owner uploads several .md files and starts the voice/chat test right away. The first test session runs with no knowledge base or execute_api while `syncDocuments`/`syncActions` are still running. The agent answers "I don't know", and the owner may click Fix and get a prompt revision aimed at a missing attachment rather than a prompt problem. — fix: call `setStep(STEP_TEST)` after both syncs settle (the warning banners already cover partial failure).

Checked and found sound:
- `enforce_prompt_structure` still guarantees both shared blocks, matched whole, and inserts them before Response style and What callers want.
- Generate's meta-prompt can produce the structure.
- The Advanced builder passes `check_prompt_structure`, and the mirror test exists (`test_guided_agent_creation.py:1222`).
- `_agent_channel` by id is correct.
- "Try again" retries only what failed (documents resume at the first un-uploaded file, and the tool-provider config is reused on retry).
- Retry buttons are disabled while busy.
- No server text reaches Easy banners.
- The wizard fix is one line and changes nothing else.
- pytest test_prompt_rules + test_agent_templates: 201 passed.
- `npx tsc --noEmit`: clean.
