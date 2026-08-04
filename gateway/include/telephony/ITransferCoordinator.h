#pragma once

#include <chrono>
#include <functional>
#include <string>

namespace voiceai {

// Shared vocabulary for both transfer strategies (see
// docs/warm_transfer_architecture.md — this header backs §2/§3 of that
// design). Cold uses only a subset (one implicit "customer" leg, no real
// LegRole/LegState tracking needed since uuid_transfer redirects that same
// leg rather than adding a second one); Warm is what actually needs the
// full vocabulary.

enum class LegRole { Customer, Agent };

enum class LegState { Originating, Ringing, Answered, Bridged, Failed, Hungup };

struct CallLeg {
    std::string uuid;
    LegRole     role;
    LegState    state;
};

// Where the caller's audio currently flows. Gateway = normal AI pipeline
// (uuid_audio_fork attached, streaming to the Conversation Service).
// BridgePending = fork stop has been issued but not yet confirmed by its
// own ESL command reply — see the sequencing note on
// ITransferCoordinator::start() below. FreeSwitch = bridged directly to
// the human leg; the Gateway is no longer in the audio path at all.
enum class MediaOwnership { Gateway, BridgePending, FreeSwitch };

enum class CoordinatorState { Idle, Active, Completed };

// Everything a coordinator needs to start one transfer attempt. Mirrors
// TransferRequest (telephony/TransferRequest.h) but is coordinator-facing
// rather than EslClient-facing — deliberately not the same struct, since
// EslClient::transfer()'s TransferRequest is a cold-only, single-command
// argument bundle that predates this interface and stays that way (Cold
// still calls it internally, unchanged).
struct TransferCoordinatorContext {
    std::string call_id;       // customer leg's FreeSWITCH uuid (== ctx_.obs.call_id)
    std::string destination;
    std::string reason;
    std::string transfer_id;   // observability-only correlation id
    // Warm-only (both default to empty/announcement_moh, which Cold simply
    // never reads): caller_id is what the agent leg's caller ID should
    // show — already resolved by CallSession to a non-empty value (the
    // Conversation Service's choice, or ctx_.caller_did if it sent none —
    // see CallSession::active_caller_id_'s own comment) by the time this
    // reaches a coordinator. waiting_experience is the raw, unresolved
    // "announcement_moh"/"announcement_silence" config value —
    // WarmTransferCoordinator itself decides whether to call hold().
    std::string caller_id;
    std::string waiting_experience;
};

// CallSession-owned side effects a coordinator triggers but does not
// perform itself — coordinators never touch transport_/fsm_ directly, only
// through these, so CallSession remains the single place that talks to the
// Conversation Service and drives CallFSM. Mirrors CallFsmHandlers' own
// "bag of callbacks injected by the owner" shape.
// TransferInitiated (the Conversation Service's one guaranteed chance to
// react, and where summary generation should eventually start — see
// docs/warm_transfer_architecture.md §7) is sent by CallSession itself,
// synchronously, before start() is ever called — identically for both
// strategies, at the earliest possible moment. It is deliberately NOT a
// coordinator callback: coordinators never need to know it exists.
struct TransferCoordinatorCallbacks {
    // Fired exactly once per attempt, however it resolves (real outcome,
    // immediately-rejected command, or the coordinator's own timeout).
    // destination empty is valid (mirrors today's CallFSM::TransferTimeout
    // convention) — CallSession falls back to the value it recorded at
    // start(). detail is the machine-readable reason
    // (e.g. "bridged", "hangup_before_bridge", "no_answer",
    // "transfer_timeout", "esl_unreachable").
    std::function<void(bool success, std::string destination, std::string detail)>
        on_transfer_completed;

    // Fired at most once per attempt, synchronously, the moment the
    // coordinator issues the FreeSWITCH command that hands the customer's
    // own SIP leg off to something outside the Gateway's control (warm:
    // right before stop_audio_fork(), which itself makes mod_audio_fork
    // close its WebSocket connection to the Gateway as a side effect;
    // cold: right after uuid_transfer is accepted, which moves the leg to
    // a new dialplan context). CallSession uses this to stop treating a
    // resulting WebSocket disconnect as an ordinary hangup — without it,
    // the disconnect-triggered session teardown races the coordinator's
    // own still-in-flight completion and can call esl_client_.hangup() on
    // a channel that's already (or about to be) live with a real human,
    // disconnecting a customer whose transfer just succeeded. May be left
    // unset — a coordinator with no such handoff step (or a test double)
    // simply never disconnects the customer's own leg this way.
    std::function<void()> on_media_handoff;
};

// One transfer strategy's orchestration, entirely decoupled from
// CallSession's other responsibilities (media/FSM/timers/websocket/gRPC/
// playback/VAD/transport — see docs/warm_transfer_architecture.md §2).
//
// Ownership: CallSession holds both concrete coordinators as value members
// (no allocation) plus one non-owning ITransferCoordinator* active_transfer_,
// set to whichever applies for the current attempt. Only start()'s callee
// needs to know the concrete type; everything downstream goes through this
// interface.
//
// Lifecycle: shutdown() is the primary safety mechanism against the exact
// bug class fixed live 2026-07-18 (a callback firing into a
// partially-destroyed CallSession) — it must be idempotent, callable from
// normal completion, CallFSM's own TransferTimeout, or ~CallSession(), and
// must guarantee no further callback fires afterward. Declaration order of
// the concrete coordinators relative to EslClient/TransferCorrelator in
// CallSession is a second, independent layer of the same guarantee — see
// docs/warm_transfer_architecture.md's "Coordinator lifetime" section.
class ITransferCoordinator {
public:
    virtual ~ITransferCoordinator() = default;

    // Begins one transfer attempt. May call callbacks.on_transfer_completed
    // synchronously (e.g. an immediately-rejected ESL command) or
    // asynchronously (the normal case — resolved later by an ESL event or
    // this coordinator's own timeout). Must not be called again for the
    // same coordinator instance until the previous attempt has reached a
    // terminal state (shutdown() called, or on_transfer_completed fired).
    virtual void start(TransferCoordinatorContext ctx,
                       TransferCoordinatorCallbacks callbacks) = 0;

    // Aborts an in-flight attempt as a failure (e.g. caller barge-in
    // cancellation during Hold — see docs/warm_transfer_architecture.md §5)
    // — fires on_transfer_completed(false, ..., "cancelled") if the attempt
    // hasn't already resolved. Safe to call when nothing is in flight
    // (no-op).
    virtual void cancel() = 0;

    // Idempotent. Unregisters any outstanding correlator/registry watches,
    // cancels any coordinator-owned timers, and guarantees no further
    // callback fires after this returns — see the class-level lifecycle
    // note above. Does NOT itself fire on_transfer_completed (the caller
    // has usually already decided the outcome by the time it calls this,
    // e.g. from ~CallSession() where no outcome will ever be reported).
    virtual void shutdown() = 0;

    [[nodiscard]] virtual CoordinatorState state() const noexcept = 0;
};

} // namespace voiceai
