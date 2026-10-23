#!/usr/bin/env python3
"""Seed the Medivista demo clinic and its two voice agents. Safe to re-run.

  python3 scripts/seed_clinic_demo.py --clinic-url http://localhost:8700 \
      [--tenant default] [--inbound-did 911205108340] [--reminder-did clinic-reminders] \
      [--reminder-caller-id 01205108340] [--hospital-phone "0120 510 8340"]

What it does, in order:
  1. Clinic data: settings, 10 departments, 24 doctors, weekly OPD hours, upcoming leave.
  2. Voices: a Deepgram nova-3 multilingual STT row and a Cartesia Hindi/English voice row
     (copying the API key refs of the tenant's existing Deepgram/Cartesia rows).
  3. Agents: `medivista-reception` (bookings + FAQ) and `medivista-reminders` (reminder calls),
     English + Hindi, created or updated in place.
  4. Tools: a fresh clinic API key (old ones revoked), the clinic custom APIs pointing at
     --clinic-url, enabled for each agent, and the execute_api policy.
  5. Phone numbers, only when --inbound-did / --reminder-did are given: points those DIDs at the agents.
     Reminder calls need FreeSWITCH to route the reminder DID to the voice AI too (see
     scripts/freeswitch/clinic_reminders_dialplan.xml).

Needs the clinic schema applied, the usual .env loaded, and toolexec allowed to reach
--clinic-url (TOOLEXEC_INTERNAL_ENDPOINTS + TOOLEXEC_HTTP_HOST_ALLOWLIST for http).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from libs.config_sdk.secrets import encrypt_tenant_secret  # noqa: E402
from libs.tenancy import set_target_tenant, tenant_conn  # noqa: E402
from services.clinic import agent_prompts, demo_data, toolauth  # noqa: E402

RECEPTION_SLUG = "medivista-reception"
REMINDER_SLUG = "medivista-reminders"
# Cartesia "Sana - Friendly Concierge": a Hindi voice that also speaks Indian English, so the
# voice does not change when the caller switches language mid-call.
HINDI_ENGLISH_VOICE = "e6b71342-48f1-4c70-a13a-d197b176ff24"


# ── 1. clinic data ──────────────────────────────────────────────────────────

async def seed_clinic(conn, tenant_id, args) -> None:
    hospital = demo_data.HOSPITAL
    await conn.execute(
        "INSERT INTO clinic_settings (tenant_id, hospital_name, hospital_name_hi, timezone, hospital_phone) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (tenant_id) DO UPDATE SET hospital_name = EXCLUDED.hospital_name, "
        "  hospital_name_hi = EXCLUDED.hospital_name_hi, timezone = EXCLUDED.timezone, "
        "  hospital_phone = COALESCE(EXCLUDED.hospital_phone, clinic_settings.hospital_phone), updated_at = now()",
        tenant_id, hospital["hospital_name"], hospital["hospital_name_hi"], hospital["timezone"], args.hospital_phone,
    )

    dept_ids = {}
    for order, (code, name, name_hi, location, description, symptoms) in enumerate(demo_data.DEPARTMENTS):
        existing = await conn.fetchval(
            "SELECT id FROM clinic_departments WHERE tenant_id = $1 AND lower(code) = lower($2) AND deleted_at IS NULL",
            tenant_id, code,
        )
        if existing:
            await conn.execute(
                "UPDATE clinic_departments SET name = $2, name_hi = $3, location = $4, description = $5, "
                "symptoms = $6, sort_order = $7, updated_at = now() WHERE id = $1",
                existing, name, name_hi, location, description, symptoms, order,
            )
            dept_ids[code] = existing
        else:
            dept_ids[code] = await conn.fetchval(
                "INSERT INTO clinic_departments (tenant_id, code, name, name_hi, location, description, symptoms, "
                "sort_order) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
                tenant_id, code, name, name_hi, location, description, symptoms, order,
            )

    doctor_ids = {}
    for (code, dept, name, name_hi, gender, quals, spec, years, langs, fee, slot, bio, schedules) in demo_data.DOCTORS:
        values = (dept_ids[dept], name, name_hi, gender, quals, spec, years, langs, fee, slot, bio)
        existing = await conn.fetchval(
            "SELECT id FROM clinic_doctors WHERE tenant_id = $1 AND lower(code) = lower($2) AND deleted_at IS NULL",
            tenant_id, code,
        )
        if existing:
            await conn.execute(
                "UPDATE clinic_doctors SET department_id = $2, name = $3, name_hi = $4, gender = $5, "
                "qualifications = $6, specialization = $7, experience_years = $8, languages = $9, "
                "consultation_fee = $10, slot_minutes = $11, bio = $12, active = true, updated_at = now() "
                "WHERE id = $1", existing, *values,
            )
            doctor_ids[code] = existing
        else:
            doctor_ids[code] = await conn.fetchval(
                "INSERT INTO clinic_doctors (tenant_id, code, department_id, name, name_hi, gender, qualifications, "
                "specialization, experience_years, languages, consultation_fee, slot_minutes, bio) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id",
                tenant_id, code, *values,
            )
        await conn.execute("DELETE FROM clinic_doctor_schedules WHERE doctor_id = $1", doctor_ids[code])
        for days, start, end in schedules:
            for weekday in days:
                await conn.execute(
                    "INSERT INTO clinic_doctor_schedules (tenant_id, doctor_id, weekday, start_time, end_time) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    tenant_id, doctor_ids[code], weekday,
                    datetime.time.fromisoformat(start), datetime.time.fromisoformat(end),
                )

    tz = ZoneInfo(demo_data.HOSPITAL["timezone"])
    today = datetime.datetime.now(tz).date()
    await conn.execute(
        "DELETE FROM clinic_doctor_leaves WHERE tenant_id = $1 AND reason LIKE '% [demo]' AND ends_at > now()",
        tenant_id,
    )
    for code, offset, days, reason in demo_data.LEAVES:
        start = datetime.datetime.combine(today + datetime.timedelta(days=offset), datetime.time.min, tzinfo=tz)
        await conn.execute(
            "INSERT INTO clinic_doctor_leaves (tenant_id, doctor_id, starts_at, ends_at, reason) "
            "VALUES ($1, $2, $3, $4, $5)",
            tenant_id, doctor_ids[code], start, start + datetime.timedelta(days=days), f"{reason} [demo]",
        )
    print(f"  clinic: {len(dept_ids)} departments, {len(doctor_ids)} doctors, {len(demo_data.LEAVES)} leave blocks")


# ── 2. voices ───────────────────────────────────────────────────────────────

async def ensure_providers(conn, tenant_id, args) -> tuple[str, str, str | None]:
    async def ensure(role, engine, name, *, model=None, voice=None, language=None, extra=None, key_ref=None):
        existing = await conn.fetchval(
            "SELECT id FROM provider_configs WHERE tenant_id = $1 AND name = $2 AND deleted_at IS NULL",
            tenant_id, name,
        )
        if existing:
            await conn.execute("UPDATE provider_configs SET voice = $2, updated_at = now() WHERE id = $1",
                               existing, voice)
            return str(existing)
        key_ref = key_ref or await conn.fetchval(
            "SELECT api_key_ref FROM provider_configs WHERE tenant_id = $1 AND engine = $2 AND role = $3 "
            "AND deleted_at IS NULL AND api_key_ref IS NOT NULL ORDER BY created_at LIMIT 1",
            tenant_id, engine, role,
        )
        if key_ref is None:
            raise SystemExit(f"no existing {engine} {role} provider with an API key to copy; add one in the console")
        return str(await conn.fetchval(
            "INSERT INTO provider_configs (tenant_id, name, role, engine, model, voice, language, api_key_ref, extra) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb) RETURNING id",
            tenant_id, name, role, engine, model, voice, language, key_ref, json.dumps(extra or {}),
        ))

    stt = await ensure("stt", "deepgram", "Deepgram nova-3 (English + Hindi)", model="nova-3")
    if args.tts == "sarvam":
        # Sarvam Bulbul: Indian-language voices; the key is read from SARVAM_API_KEY in .env.
        tts = await ensure("tts", "sarvam", "Sarvam Bulbul (Hindi + English)", model="bulbul:v4-flash",
                           voice=args.sarvam_speaker, extra={"model": "bulbul:v4-flash"},
                           key_ref="env:SARVAM_API_KEY")
    else:
        tts = await ensure("tts", "cartesia", "Cartesia Sana (Hindi + English)", voice=HINDI_ENGLISH_VOICE,
                           extra={"model": "sonic-2"})
    llm = await conn.fetchval(
        "SELECT default_llm_config_id FROM tenants WHERE id = $1", tenant_id,
    )
    print(f"  voices: stt={stt} tts={tts} llm={'tenant default' if llm else 'none'}")
    return stt, tts, (str(llm) if llm else None)


# ── 3. agents ───────────────────────────────────────────────────────────────

def _department_guide() -> str:
    return "\n".join(f"- {name} ({code}): {', '.join(symptoms)}"
                     for code, name, _hi, _loc, _desc, symptoms in demo_data.DEPARTMENTS)


async def upsert_agent(tenant_id, tenant_slug, *, slug, name, prompt, greeting_en, greeting_hi, stt, tts, llm):
    from services.config import agents

    fields = dict(
        name=name, system_prompt=prompt, greeting=greeting_en, stt_config_id=stt, tts_config_id=tts,
        language="en", supported_languages=["en", "hi"], language_mode="multilingual",
        greeting_by_language={"en": greeting_en, "hi": greeting_hi}, tts_config_by_language={"hi": tts},
    )
    if llm:
        fields["llm_config_id"] = llm
    pool = await agents.db.get_pool()
    async with tenant_conn(pool) as conn:
        existing = await conn.fetchval(
            "SELECT id FROM agents WHERE tenant_id = $1 AND slug = $2 AND deleted_at IS NULL", tenant_id, slug,
        )
    if existing:
        await agents.update_agent(existing, tenant_slug=tenant_slug, user_email="seed_clinic_demo", **fields)
        print(f"  agent updated: {slug}")
        return str(existing)
    created = await agents.create_agent(tenant_id=tenant_id, tenant_slug=tenant_slug, slug=slug,
                                        user_email="seed_clinic_demo", **fields)
    print(f"  agent created: {slug}")
    return str(created["id"])


# ── 4. tools ────────────────────────────────────────────────────────────────

def _p(name, description="", *, required=False, source="caller", json_type="string", upstream=None, path=None):
    return {"name": name, "location": "body", "json_type": json_type, "description": description,
            "required": required, "source": source, "upstream": upstream, "upstream_json_path": path,
            "sensitive": source == "caller_id"}


def _parts(path: str) -> str:
    """A template that speaks every part of a chunked sentence (see booking.speech_parts)."""
    return " ".join(f"{{{{{path}_{i}}}}}" for i in range(1, 5))


LANG = _p("language", 'The caller\'s current language: "en" for English, "hi" for Hindi or Hinglish.')
CALLER = _p("caller_phone", source="caller_id")

# name, path, side_effecting, description, params, confirmation_template, success_template, agents
APIS = [
    ("clinic_list_departments", "/departments", False,
     "List the hospital's departments, what each one treats, where it is, and how many doctors it has.",
     [LANG], None, None, ("reception", "reminders")),
    ("clinic_find_doctors", "/doctors", False,
     "Look up doctors by name or department, with qualifications, experience, languages, fee and location.",
     [_p("doctor", "Doctor name or doctor_code."), _p("department", "Department name or code."),
      _p("consult_language", 'Only doctors who consult in this language: "en" or "hi".'), LANG],
     None, None, ("reception", "reminders")),
    ("clinic_check_availability", "/availability", False,
     "Free appointment times in the next 15 days for one doctor, or for the doctors of one department.",
     [_p("doctor", "Doctor name or doctor_code; give this or department."),
      _p("department", "Department name or code; give this or doctor."),
      _p("date", "Only this day: YYYY-MM-DD, or the caller's words such as \"thursday\", \"tomorrow\", "
                 "\"कल\". Leave out to get the earliest times."),
      _p("part_of_day", '"morning", "afternoon" or "evening", if the caller has a preference.'), LANG],
     None, None, ("reception", "reminders")),
    ("clinic_prepare_booking", "/bookings/prepare", False, "Checks and holds a slot before booking (internal step).",
     [_p("doctor", "The doctor_code of the chosen doctor.", required=True),
      _p("slot_start", "The chosen slot_start, exactly as clinic_check_availability returned it.", required=True),
      _p("patient_name", "The patient's full name.", required=True),
      _p("patient_phone", "Only when booking on a different mobile number than the one calling."), LANG, CALLER],
     None, None, ()),
    ("clinic_book_appointment", "/bookings", True,
     "Book an appointment. The details are read back to the caller first; call again with the same inputs "
     "once they say yes.",
     [_p("doctor_code", source="upstream", upstream="clinic_prepare_booking", path="$.doctor_code"),
      _p("slot_start", source="upstream", upstream="clinic_prepare_booking", path="$.slot_start"),
      _p("patient_phone", source="upstream", upstream="clinic_prepare_booking", path="$.patient_phone"),
      _p("patient_name", "The patient's full name.", required=True),
      _p("reason", "The health concern in a few words."), _p("patient_age", "Patient age in years, if given."),
      LANG, CALLER],
     _parts("$.upstream.clinic_prepare_booking.confirm_text"), _parts("$.spoken"), ("reception",)),
    ("clinic_find_appointments", "/appointments/find", False,
     "Find the caller's upcoming appointments. From another phone, pass the booking number and the mobile "
     "number used for the booking.",
     [_p("reference", "The 6-digit booking number, if the caller has it."),
      _p("patient_phone", "The mobile number used for the booking, when calling from a different phone."),
      LANG, CALLER],
     None, None, ("reception", "reminders")),
    ("clinic_prepare_reschedule", "/reschedule/prepare", False, "Checks and holds the new slot (internal step).",
     [_p("reference", "The booking number.", required=True),
      _p("new_slot_start", "The chosen new slot_start, exactly as clinic_check_availability returned it.",
         required=True),
      _p("doctor", "Only when moving to a different doctor: that doctor's doctor_code."),
      _p("patient_phone", "The mobile number used for the booking, when calling from a different phone."),
      LANG, CALLER],
     None, None, ()),
    ("clinic_reschedule_appointment", "/reschedule", True,
     "Move an existing appointment to a new time, with the same or a different doctor; the booking number "
     "stays the same. The change is read back first; call again with the same inputs once the caller says yes.",
     [_p("reference", source="upstream", upstream="clinic_prepare_reschedule", path="$.reference"),
      _p("new_slot_start", source="upstream", upstream="clinic_prepare_reschedule", path="$.new_slot_start"),
      _p("doctor_code", source="upstream", upstream="clinic_prepare_reschedule", path="$.doctor_code"),
      _p("patient_phone", "The mobile number used for the booking, when calling from a different phone."),
      LANG, CALLER],
     _parts("$.upstream.clinic_prepare_reschedule.confirm_text"), _parts("$.spoken"), ("reception", "reminders")),
    ("clinic_prepare_cancel", "/cancel/prepare", False, "Checks the booking before cancelling (internal step).",
     [_p("reference", "The booking number.", required=True),
      _p("patient_phone", "The mobile number used for the booking, when calling from a different phone."),
      LANG, CALLER],
     None, None, ()),
    ("clinic_cancel_appointment", "/cancel", True,
     "Cancel an appointment. The cancellation is read back first; call again with the same inputs once the "
     "caller says yes.",
     [_p("reference", source="upstream", upstream="clinic_prepare_cancel", path="$.reference"),
      _p("patient_phone", "The mobile number used for the booking, when calling from a different phone."),
      LANG, CALLER],
     _parts("$.upstream.clinic_prepare_cancel.confirm_text"), _parts("$.spoken"), ("reception", "reminders")),
    ("clinic_reminder_details", "/reminder/context", False,
     "The appointment this reminder call is about, with a ready sentence to read out.",
     [LANG, CALLER], None, None, ("reminders",)),
    ("clinic_reminder_response", "/reminder/outcome", True,
     "Record whether the patient will come to the appointment this reminder is about.",
     [_p("reference", "The booking number from clinic_reminder_details.", required=True),
      _p("outcome", '"confirmed" if they will come, "declined" if they cannot.', required=True),
      LANG, CALLER],
     None, _parts("$.spoken"), ("reminders",)),
]


async def seed_tools(tenant_id, clinic_url: str, agent_ids: dict[str, str]) -> None:
    from services.toolexec import custom_apis, db as toolexec_db

    key = toolauth.generate_tool_key()
    pool = await toolexec_db.get_pool()
    base = clinic_url.rstrip("/") + "/tool/v1"
    await custom_apis.resolve_and_validate_endpoint(base + "/departments")  # fail early if toolexec can't reach it

    async with tenant_conn(pool) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('custom_apis:' || $1::text))", str(tenant_id))
        await conn.execute("UPDATE clinic_api_keys SET revoked_at = now() WHERE tenant_id = $1 AND revoked_at IS NULL",
                           tenant_id)
        await conn.execute("INSERT INTO clinic_api_keys (tenant_id, key_hash, label) VALUES ($1, $2, 'toolexec')",
                           tenant_id, toolauth.hash_tool_key(key))
        await conn.execute(
            "UPDATE custom_apis SET deleted_at = now() WHERE tenant_id = $1 AND name LIKE 'clinic\\_%' "
            "AND deleted_at IS NULL", tenant_id,
        )
        auth_config = {"token_ref": encrypt_tenant_secret(tenant_id, key)}
        ids: dict[str, str] = {}
        for name, path, side_effecting, description, params, confirm, success, _agents in APIS:
            rows = [{**p, "upstream_api_id": ids[p["upstream"]] if p["upstream"] else None} for p in params]
            created = await custom_apis._insert_custom_api(
                conn, tenant_id=tenant_id, name=name, description=description, endpoint_url=base + path,
                method="POST", body_style="json", auth_scheme="bearer", auth_config=auth_config,
                side_effecting=side_effecting, idempotency_header=None, timeout_ms=5000,
                sensitive_response_paths=[], success_template=success, params=rows,
                confirmation_template=confirm,
            )
            ids[name] = str(created["id"])
        await custom_apis._recompute_tenant_chain_levels(conn, tenant_id)

        tool_config = await conn.fetchval(
            "SELECT id FROM tool_provider_configs WHERE tenant_id = $1 AND tool_name = 'execute_api' "
            "AND engine = 'toolexec' AND deleted_at IS NULL LIMIT 1", tenant_id,
        ) or await conn.fetchval(
            "INSERT INTO tool_provider_configs (tenant_id, name, tool_name, engine) "
            "VALUES ($1, 'Tool Execution Service', 'execute_api', 'toolexec') RETURNING id", tenant_id,
        )
        for role, agent_id in agent_ids.items():
            await conn.execute(
                "INSERT INTO agent_tool_policies (agent_id, tool_name, tool_provider_config_id, enabled, max_chain_depth) "
                "VALUES ($1, 'execute_api', $2, true, 2) "
                "ON CONFLICT (agent_id, tool_name) DO UPDATE SET enabled = true, updated_at = now()",
                agent_id, tool_config,
            )
            for name, *_rest, agents in APIS:
                if role in agents:
                    await conn.execute(
                        "INSERT INTO agent_custom_apis (agent_id, custom_api_id, enabled) VALUES ($1, $2, true) "
                        "ON CONFLICT (agent_id, custom_api_id) DO UPDATE SET enabled = true, updated_at = now()",
                        agent_id, ids[name],
                    )
    print(f"  tools: {len(APIS)} clinic APIs at {base}, new tool key issued (previous keys revoked)")


# ── 5. phone numbers ────────────────────────────────────────────────────────

async def bind_number(tenant_id, did: str, agent_id: str, *, create: bool = False) -> None:
    """Through config's phone_numbers module, which also rewrites the Gateway's Redis route;
    a plain UPDATE would leave calls on the old agent."""
    from libs.telephony_sdk.did_format import normalize_did
    from services.config import db as config_db, phone_numbers

    pool = await config_db.get_pool()
    async with tenant_conn(pool) as conn:
        number_id = await conn.fetchval(
            "SELECT id FROM phone_numbers WHERE tenant_id = $1 AND did = $2 AND deleted_at IS NULL",
            tenant_id, normalize_did(did),
        )
    if number_id is None:
        if not create:
            raise SystemExit(f"DID {did} is not one of this tenant's phone numbers")
        await phone_numbers.create_phone_number(tenant_id=tenant_id, did=did, agent_id=agent_id,
                                                user_email="seed_clinic_demo")
    else:
        await phone_numbers.update_phone_number(number_id, agent_id=agent_id, user_email="seed_clinic_demo")
    print(f"  phone: {normalize_did(did)} -> agent {agent_id}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--clinic-url", required=True, help="How toolexec reaches the clinic service")
    parser.add_argument("--hospital-phone", help="Front-desk number quoted in SMS")
    parser.add_argument("--inbound-did", help="Point this DID at the reception agent")
    parser.add_argument("--reminder-did", help="Route answered reminder calls to the reminder agent through this DID, "
                                               "or an internal-only name such as clinic-reminders (recommended)")
    parser.add_argument("--reminder-caller-id", help="Caller ID shown to patients on reminder calls")
    parser.add_argument("--tts", choices=("cartesia", "sarvam"), default="cartesia",
                        help="Voice provider for both agents (sarvam needs SARVAM_API_KEY in .env)")
    parser.add_argument("--sarvam-speaker", default="simran_enhi_customer",
                        help="bulbul:v4-flash persona, e.g. simran_enhi_customer, ishita_enhi_customer")
    args = parser.parse_args()

    from services.clinic import db as clinic_db
    pool = await clinic_db.get_pool()
    async with tenant_conn(pool, explicit_tenant=args.tenant, reason="seed-clinic-demo") as conn:
        tenant_id = str(await conn.fetchval("SELECT current_setting('app.tenant_id')"))
    set_target_tenant(tenant_id)
    print(f"Seeding clinic demo into tenant {args.tenant} ({tenant_id})")

    async with tenant_conn(pool) as conn:
        await seed_clinic(conn, tenant_id, args)
        stt, tts, llm = await ensure_providers(conn, tenant_id, args)

    hospital = demo_data.HOSPITAL
    faq = (REPO / "docs/clinic/medivista-faq.md").read_text().split("\n", 2)[2].strip()
    fill = dict(hospital=hospital["hospital_name"], hospital_hi=hospital["hospital_name_hi"], faq=faq,
                departments=_department_guide(), window=15)
    agent_ids = {
        "reception": await upsert_agent(
            tenant_id, args.tenant, slug=RECEPTION_SLUG, name="Medivista Reception",
            prompt=agent_prompts.RECEPTIONIST_PROMPT.format(**fill),
            greeting_en=agent_prompts.RECEPTIONIST_GREETING_EN.format(**fill),
            greeting_hi=agent_prompts.RECEPTIONIST_GREETING_HI.format(**fill), stt=stt, tts=tts, llm=llm),
        "reminders": await upsert_agent(
            tenant_id, args.tenant, slug=REMINDER_SLUG, name="Medivista Reminders",
            prompt=agent_prompts.REMINDER_PROMPT.format(**fill),
            greeting_en=agent_prompts.REMINDER_GREETING_EN.format(**fill),
            greeting_hi=agent_prompts.REMINDER_GREETING_HI.format(**fill), stt=stt, tts=tts, llm=llm),
    }
    await seed_tools(tenant_id, args.clinic_url, agent_ids)

    if args.inbound_did:
        await bind_number(tenant_id, args.inbound_did, agent_ids["reception"])
    if args.reminder_did:
        # A name like "clinic-reminders" is an internal-only route, created on first use.
        await bind_number(tenant_id, args.reminder_did, agent_ids["reminders"],
                          create=not any(c.isdigit() for c in args.reminder_did))
    async with tenant_conn(pool) as conn:
        if args.reminder_did:
            await conn.execute(
                "UPDATE clinic_settings SET reminder_agent_did = $2, "
                "reminder_caller_id = COALESCE($3, reminder_caller_id), updated_at = now() WHERE tenant_id = $1",
                tenant_id, args.reminder_did, args.reminder_caller_id,
            )
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
