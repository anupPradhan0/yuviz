as "connector presets + OAuth2 authorization-code auth make sure you cosider all the points"?

---

## The points referenced ("all the points")

The line above is the user's request, verbatim. "All the points" refers to the analysis agreed in the conversation that produced it, recorded here so every stage sees it:

**Target customers:** small businesses and hospitals/clinics in India. Mostly inbound reception and appointment calls, plus simple outbound reminders.

**Decision:** booking stays on the existing generic `execute_api` tool (tenant-registered custom APIs in services/toolexec). The calendar/SMS built-ins that were retired on 2026-09-18 are NOT coming back. The gap is usability, not the tool.

1. **Per-tenant OAuth2 authorization-code auth.** Today the auth schemes are api_key, bearer and oauth2_client_credentials. Google Calendar, Zoho and Microsoft/Outlook need a "Connect <provider>" button: authorization-code flow, per-tenant access and refresh tokens stored as tenant-namespaced secrets, automatic refresh, and disconnect/revoke. Refresh tokens are highly sensitive tenant data; tenant isolation (RLS) is non-negotiable.
2. **One-click connector presets.** A small-business owner cannot fill in an endpoint URL, parameter schema, chaining or success templates. Choosing a preset such as "Google Calendar booking" should create the linked custom APIs already configured (check slots → book → reschedule → cancel, using the existing upstream_api_id chaining) and make them available to the Appointments step of an agent's call flow. Candidate presets: Google Calendar booking, WhatsApp confirmation (Gupshup / Interakt / Meta Cloud API), Google Sheets lead capture. Later: Zoho Bookings, Cal.com, Practo, KareXpert.
3. **Clinics with no software** have no API to call. A Yuviz-hosted simple scheduler, exposed as an API that execute_api calls, is a planned follow-up. The PRD should decide whether it is in scope or explicitly deferred.
4. **Voice-specific booking concerns:**
   - Idempotency: a key per call and per booking so a retried or timed-out call never double-books.
   - Spoken output: offer 2–3 slots, not 20. Handle this in the preset's success templates, not the prompt.
   - Latency: slot lookups must be fast. Use the existing filler phrases when a lookup exceeds about 700 ms.
   - Confirm before booking: read back name, doctor and time, and get a "yes" before calling the booking API.
5. **Security:** the existing SSRF guard (checked at registration and before every call) must also cover preset endpoints and OAuth token and authorization endpoints. OAuth state and PKCE are needed, and so is redirect URI validation. Tokens must never be logged or shown in the UI, transcripts or audit rows.
6. **Goal:** a clinic can go live with booking and WhatsApp confirmations in about 10 minutes, with no developer.
