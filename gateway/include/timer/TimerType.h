#pragma once

#include <cstdint>
#include <string_view>

namespace voiceai {

// All timer types used by the gateway.  One timer fires per state per session.
// See CallFSM::on_timer_fired() for the dispatch table.
enum class TimerType : uint8_t {
    ConnectionTimeout,   // Connecting state
    NoSpeechTimeout,     // Listening state
    MaxUtteranceTimeout, // Recognizing state, before speech_ended (bounds raw talk time)
    SttTimeout,          // Recognizing state, after speech_ended (bounds STT response time)
    LlmTimeout,          // Thinking state
    TtsTimeout,          // Synthesizing state
    PlaybackTimeout,     // Speaking state
    GoodbyeTimeout,      // WaitingForHangup state
    GoodbyeConfirm,      // WaitingForHangup state, after a SpeechStarted onset
    BargeInWindow,       // BargeIn state
    TransferTimeout,     // Transferring state
    FinalizingTimeout,   // Finalizing state
    CloseTimeout,        // Closing state
    RtpInactivity,       // Any — heartbeat
};

[[nodiscard]] inline std::string_view to_string(TimerType t) noexcept {
    switch (t) {
    case TimerType::ConnectionTimeout: return "ConnectionTimeout";
    case TimerType::NoSpeechTimeout:   return "NoSpeechTimeout";
    case TimerType::MaxUtteranceTimeout: return "MaxUtteranceTimeout";
    case TimerType::SttTimeout:        return "SttTimeout";
    case TimerType::LlmTimeout:        return "LlmTimeout";
    case TimerType::TtsTimeout:        return "TtsTimeout";
    case TimerType::PlaybackTimeout:   return "PlaybackTimeout";
    case TimerType::GoodbyeTimeout:    return "GoodbyeTimeout";
    case TimerType::GoodbyeConfirm:    return "GoodbyeConfirm";
    case TimerType::BargeInWindow:     return "BargeInWindow";
    case TimerType::TransferTimeout:   return "TransferTimeout";
    case TimerType::FinalizingTimeout: return "FinalizingTimeout";
    case TimerType::CloseTimeout:      return "CloseTimeout";
    case TimerType::RtpInactivity:     return "RtpInactivity";
    }
    return "Unknown";
}

} // namespace voiceai
