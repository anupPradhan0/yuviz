"""
Pydantic request models — validate what comes in over HTTP before it reaches
Config Service. Responses are the plain dicts tenants.py/agents.py/
provider_configs.py already return (FastAPI serializes UUID/datetime in a
dict automatically) — no separate response schema, so there's exactly one
place field lists are maintained, not two that can drift apart.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str
    slug: str
    region: str = "us"


class TenantUpdate(BaseModel):
    name:                  str | None = None
    region:                str | None = None
    vad_engine:            str | None = None
    vad_onset_ms:          int | None = None
    vad_hold_ms:           int | None = None
    vad_speech_threshold:  float | None = None
    no_speech_timeout_ms:  int | None = None
    stt_timeout_ms:        int | None = None
    llm_timeout_ms:        int | None = None
    # Bounds mirror the gateway's CallFsmTimerConfig::transfer_timeout_min/max
    # (10s-120s) — enforced here too so a bad value is rejected at config
    # time instead of silently falling back to the default at call time.
    transfer_timeout_ms:   int | None = Field(default=None, ge=10_000, le=120_000)
    default_stt_config_id: str | None = None
    default_llm_config_id: str | None = None
    default_tts_config_id: str | None = None


class AgentCreate(BaseModel):
    slug:           str
    name:           str
    greeting:       str = ""
    system_prompt:  str = ""
    stt_config_id:  str | None = None
    llm_config_id:  str | None = None
    tts_config_id:  str | None = None


class AgentUpdate(BaseModel):
    name:                 str | None = None
    greeting:             str | None = None
    system_prompt:        str | None = None
    goodbye_grace_ms:     int | None = None
    language:             str | None = None
    stt_config_id:        str | None = None
    llm_config_id:        str | None = None
    tts_config_id:        str | None = None
    transfer_type:        Literal["warm", "cold", "none"] | None = None
    transfer_destination: str | None = None
    queue_id:             str | None = None
    escalation_threshold: int | None = None
    # What caller ID the human agent sees on a warm transfer's agent leg —
    # resolved entirely in the Conversation Service; the gateway never sees
    # this, only the final caller_id string (see transfer_engine.py).
    caller_id_policy:     Literal["original", "platform", "custom"] | None = None
    platform_did:         str | None = None
    custom_caller_id:     str | None = None
    transfer_waiting_experience: Literal["announcement_moh", "announcement_silence"] | None = None
    # Condition-clause overrides for the [[END_CALL]]/[[TRANSFER]] trigger
    # instructions; None/empty = built-in defaults (see pipeline.py).
    end_call_prompt:      str | None = None
    transfer_prompt:      str | None = None
    # Exact scripted lines the agent speaks when ending/transferring;
    # None/empty = LLM chooses the wording (see pipeline.py).
    farewell_message:      str | None = None
    transfer_announcement: str | None = None
    status:               Literal["active", "inactive"] | None = None


class ProviderConfigCreate(BaseModel):
    name:        str
    # 'embedding' added for Phase 6A's Knowledge Platform (services/knowledge/
    # embedding_manager.py) — provider_configs.role's CHECK constraint
    # already allows it (see database/knowledge_schema.sql); this schema
    # just hadn't been widened to match until now.
    role:        Literal["stt", "llm", "tts", "embedding"]
    engine:      str
    environment: Literal["prod", "staging", "dev"] = "prod"
    model:       str | None = None
    voice:       str | None = None
    language:    str | None = None
    region:      str | None = None
    api_key_ref: str | None = None
    extra:       dict[str, Any] | None = None


class ProviderConfigUpdate(BaseModel):
    name:        str | None = None
    engine:      str | None = None
    environment: Literal["prod", "staging", "dev"] | None = None
    model:       str | None = None
    voice:       str | None = None
    language:    str | None = None
    region:      str | None = None
    api_key_ref: str | None = None


class TelephonyConfigCreate(BaseModel):
    name:                str
    # Validated against libs.telephony_sdk's registered provider names at
    # the business-logic layer (services/config/telephony_configs.py), not
    # here — new providers are additive there, not a schema change here.
    provider:            str
    credentials:         dict[str, Any] = {}
    is_default_outbound: bool = False


class TelephonyConfigUpdate(BaseModel):
    name:                str | None = None
    # provider is immutable after creation — same posture as
    # ProviderConfigUpdate dropping `role`.
    credentials:         dict[str, Any] | None = None
    is_default_outbound: bool | None = None


class ToolProviderConfigCreate(BaseModel):
    name:        str
    # 'tool_name' matches the static catalog in services/conversation/tools/
    # registry.py (ToolRegistry) — only "book_appointment" exists there today.
    tool_name:   str
    engine:      str
    # Required, unlike provider_configs.api_key_ref: every tool engine that
    # exists today (cal_com) is a cloud API requiring credentials — the
    # runtime already hard-fails without one (see provider_manager.py's
    # _make_cal_com), but only at call time, which is exactly what let a
    # blank-key config through the Admin UI and silently broke a live call
    # (2026-07-23). Caught here instead, at creation time. If a keyless
    # engine is ever added, revisit this back to optional + a per-engine
    # check, matching provider_configs' own per-role posture.
    api_key_ref: str
    extra:       dict[str, Any] | None = None


class ToolProviderConfigUpdate(BaseModel):
    name:        str | None = None
    engine:      str | None = None
    api_key_ref: str | None = None
    extra:       dict[str, Any] | None = None


class AgentToolPolicyCreate(BaseModel):
    tool_name:                str
    tool_provider_config_id:  str
    enabled:                  bool = True
    timeout_ms:               int | None = None
    max_calls_per_turn:       int | None = None


class AgentToolPolicyUpdate(BaseModel):
    enabled:             bool | None = None
    timeout_ms:          int | None = None
    max_calls_per_turn:  int | None = None
    extra:       dict[str, Any] | None = None


class CarrierCreate(BaseModel):
    name:                 str
    provider:             Literal["twilio", "plivo", "vonage"]
    auth_id:              str | None = None
    auth_token_ref:       str | None = None
    carrier_account_ref:  str | None = None


class CarrierUpdate(BaseModel):
    name:                 str | None = None
    auth_id:              str | None = None
    auth_token_ref:       str | None = None
    carrier_account_ref:  str | None = None


class PhoneNumberCreate(BaseModel):
    did:               str
    agent_id:          str | None = None
    fallback_agent_id: str | None = None
    carrier_id:        str | None = None
    region:            str | None = None
    status:            Literal["active", "inactive", "suspended"] = "active"


class PhoneNumberUpdate(BaseModel):
    did:               str | None = None
    agent_id:          str | None = None
    fallback_agent_id: str | None = None
    carrier_id:        str | None = None
    region:            str | None = None
    status:            Literal["active", "inactive", "suspended"] | None = None


class LoginRequest(BaseModel):
    email:    str
    password: str


class BootstrapRequest(BaseModel):
    email:    str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8)


class UserCreate(BaseModel):
    email:     str
    password:  str
    role:      Literal["superadmin", "admin", "viewer"] = "admin"
    tenant_id: str | None = None  # None == superadmin scope


class UserUpdate(BaseModel):
    role:      Literal["superadmin", "admin", "viewer"] | None = None
    tenant_id: str | None = None
    password:  str | None = Field(default=None, min_length=8)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:      str = Field(min_length=8)
