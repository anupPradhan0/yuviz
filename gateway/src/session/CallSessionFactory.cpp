#include "session/CallSessionFactory.h"
#include "media/EnergyVAD.h"
#include "media/MediaSession.h"
#include "media/SileroVAD.h"

namespace voiceai {

CallSessionFactory::CallSessionFactory(ConversationTransportFactory& transport_factory,
                                       AudioWorkerPool&              worker_pool,
                                       ITimerService&                timer_svc,
                                       IDispatcher&                  dispatcher,
                                       IMetrics&                     metrics,
                                       IClock&                       clock,
                                       Logger&                       logger,
                                       EslClient&                    esl_client,
                                       TransferCorrelator&            transfer_correlator,
                                       TransferCorrelator&            job_correlator)
    : transport_factory_(transport_factory)
    , worker_pool_      (worker_pool)
    , timer_svc_        (timer_svc)
    , dispatcher_       (dispatcher)
    , metrics_          (metrics)
    , clock_            (clock)
    , logger_           (logger)
    , esl_client_       (esl_client)
    , transfer_correlator_(transfer_correlator)
    , job_correlator_   (job_correlator)
{}

std::unique_ptr<CallSession> CallSessionFactory::create(
    SessionContext                        ctx,
    std::shared_ptr<IWebSocketConnection> connection)
{
    const TenantConfig& tc  = *ctx.tenant;
    const std::string&  sid = ctx.obs.session_id;

    std::unique_ptr<IVAD> vad;
    if (tc.vad_engine == "silero") {
        try {
            vad = std::make_unique<SileroVAD>(tc.silero, logger_);
        } catch (const std::exception& e) {
            logger_.warn("SileroVAD unavailable ({}); falling back to EnergyVAD",
                         e.what());
        }
    }
    if (!vad) vad = std::make_unique<EnergyVAD>(tc.vad);

    // Convert frame count → ms for the MediaSession ring buffer.
    const uint32_t ring_ms = static_cast<uint32_t>(
        tc.backpressure.max_inbound_queue_frames * tc.frame_ms);

    auto media = std::make_unique<MediaSession>(
        sid,
        tc.sample_rate,
        tc.channels,
        ring_ms,
        tc.frame_ms,
        std::move(vad),
        tc.backpressure.max_outbound_queue_frames,
        metrics_,
        clock_,
        logger_);

    auto transport = transport_factory_.create(
        tc.transport.type.empty() ? "null" : tc.transport.type,
        logger_);

    return std::make_unique<CallSession>(
        std::move(ctx),
        std::move(connection),
        std::move(media),
        worker_pool_,
        std::move(transport),
        timer_svc_,
        dispatcher_,
        metrics_,
        clock_,
        logger_,
        esl_client_,
        transfer_correlator_,
        job_correlator_);
}

} // namespace voiceai
