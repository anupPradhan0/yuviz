#pragma once

#include <chrono>

namespace voiceai {

struct CallFsmTimerConfig {
    std::chrono::milliseconds connection_timeout{10'000};
    // Per-tenant configurable via the tenant:{id} Redis overlay
    // ("no_speech_timeout_ms", see TenantConfig::from_redis); values outside
    // [min,max] fall back to the default with a warning — same discipline as
    // transfer_timeout below. Confirmed live 2026-07-21: a caller who goes
    // silent for the full window is hung up deliberately, by design — this
    // constant is the actual dial, not a bug to work around.
    static constexpr std::chrono::milliseconds no_speech_timeout_min    {5'000};
    static constexpr std::chrono::milliseconds no_speech_timeout_default{30'000};
    static constexpr std::chrono::milliseconds no_speech_timeout_max    {120'000};
    std::chrono::milliseconds no_speech_timeout {no_speech_timeout_default};
    // Bounds Recognizing before speech_ended fires — i.e. how long the caller
    // may keep talking in one breath.  Deliberately generous: this is a
    // pathological-case safety net (stuck-open mic, VAD never detecting a
    // pause), not a UX-facing limit.  Must stay well above realistic
    // utterance lengths; do not conflate with stt_timeout below.
    std::chrono::milliseconds max_utterance_timeout {45'000};
    // Bounds Recognizing after speech_ended fires — i.e. how long STT itself
    // may take to produce a result, once it actually has the full utterance.
    std::chrono::milliseconds stt_timeout        {8'000};
    std::chrono::milliseconds llm_timeout       {20'000};
    std::chrono::milliseconds tts_timeout       {10'000};
    std::chrono::milliseconds playback_timeout  {60'000};
    // Grace period after the agent's goodbye finishes playing, during which
    // the caller may speak up to cancel the pending hangup (WaitingForHangup
    // → Listening).  If it expires with no speech detected, the gateway
    // issues the actual SIP hangup via ESL.
    std::chrono::milliseconds goodbye_timeout   {2'500};
    // A bare VAD onset during WaitingForHangup (background noise, a breath,
    // a chair creak) must not immediately cancel a legitimate goodbye —
    // only genuinely sustained speech should. On SpeechStarted, the
    // GoodbyeTimeout is swapped for this short confirm window; if speech
    // is still ongoing when it fires, the hangup is cancelled for real. If
    // notify_speech_ended arrives first (the onset was just a blip), the
    // full goodbye_timeout grace window is restored instead.
    std::chrono::milliseconds goodbye_confirm     {300};
    std::chrono::milliseconds barge_in_window     {500};
    // How long Transferring may wait for a CHANNEL_BRIDGE/CHANNEL_HANGUP
    // before the attempt is declared failed (reason=transfer_timeout). Must
    // exceed the dialplan's own ring timeout (call_timeout=30s in
    // Local_Extension) plus SIP setup, or an answer in the late-ring window
    // is declared failed while the phone is still ringing. Per-tenant
    // configurable via the tenant:{id} Redis overlay ("transfer_timeout_ms",
    // see TenantConfig::from_redis); values outside [min,max] fall back to
    // the default with a warning.
    static constexpr std::chrono::milliseconds transfer_timeout_min    {10'000};
    static constexpr std::chrono::milliseconds transfer_timeout_default{45'000};
    static constexpr std::chrono::milliseconds transfer_timeout_max   {120'000};
    std::chrono::milliseconds transfer_timeout  {transfer_timeout_default};
    // Safety net if the Conversation Service's ConversationFinalized
    // acknowledgement never arrives after a successful transfer (Phase 5D)
    // — longer than transfer_timeout since finalization includes an LLM
    // summary call, not just a few DB writes.
    std::chrono::milliseconds finalizing_timeout {15'000};
    std::chrono::milliseconds close_timeout      {5'000};
};

} // namespace voiceai
