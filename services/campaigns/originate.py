"""
originate.py — commands FreeSWITCH to place a real outbound PSTN call and
route it into the existing AI pipeline, entirely via a raw ESL (Event
Socket) connection.

CORRECTED 2026-07-28, against a real Kamailio + FreeSWITCH + Zoiper
end-to-end test — the original design here (routing the answered leg by
locally executing the agent's DID as a dialplan extension, via
`<caller_id> XML default`) was WRONG and is kept only in git history.
Read on for what was actually found and why the fix below works.

Why this file exists instead of reusing the C++ Gateway's EslClient: the
Gateway has no reachable control surface at all — it only (a) accepts
FreeSWITCH's WebSocket connection for media, (b) talks to Conversation
Service via gRPC, (c) talks to FreeSWITCH via ESL. Nothing lets an HTTP
request from a separate service reach into the Gateway and say "place a
call." Building a new C++ control-plane endpoint was the bigger, slower
option; this instead speaks the exact same ESL protocol directly from
Python — a raw text protocol (mod_event_socket), simple enough to
implement fresh without a dependency, mirroring the same "thin bridge
alongside the existing system" pattern already used for services/webcall/.

What a real test call revealed by reading Kamailio's OWN active routing
rules (/usr/local/etc/kamailio/kamailio.cfg — a file with a lot of dead,
commented-out history, so grep with care): a call whose SIP Request-URI
is `788` or `5000`-`5009` gets relayed unconditionally straight to
FreeSWITCH's port 5080 (`if ($rU == "788" || $rU =~ "500[0-9]") { ...
$ru = "sip:" + $rU + "@192.168.0.116:5080"; route(RELAY); }`) — THAT is
the real path a genuine inbound call to an agent's DID (e.g. "5006")
takes, confirmed by 148 real `calls` rows with caller_number=1001,
called_number=5006 in Postgres, all originating from a phone dialing the
DID directly. A call whose Request-URI is `1000`-`1002` (a registered
Zoiper/Linphone extension) hits a completely different gatekeeper block.

The original design's mistake: it dialed the CONTACT (e.g. "1001") as the
Request-URI — correctly reaching the 1000-1002 gatekeeper, which is why
the phone genuinely rang — but then tried to reach the agent's DID via
`<caller_id> XML default`, which does NOT send a new SIP INVITE back out
to Kamailio at all. It just tells FreeSWITCH to locally execute an
extension named "5006" inside its own stock, unmodified `default`
dialplan context, which has nothing in it for that number, so the call
silently dead-ended every time (confirmed via FreeSWITCH's own call log:
"has executed the last dialplan instruction, hanging up").

The fix: once the contact answers, BRIDGE to a fresh leg addressed
directly to the agent's DID, sent back out to Kamailio exactly the way a
real inbound call would be — `&bridge(sofia/external/sip:<caller_id>@
<sip_proxy_host>:<sip_proxy_port>)` instead of `<caller_id> XML default`.
That new INVITE's Request-URI (the DID) hits Kamailio's `500[0-9]` rule
and lands on the exact same path real inbound calls already take.

ESL command shape:
    bgapi originate {origination_caller_id_number=<caller_id>}sofia/external/sip:<phone_number>@<sip_proxy_host>:<sip_proxy_port> &bridge(sofia/external/sip:<caller_id>@<sip_proxy_host>:<sip_proxy_port>)
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

_ESL_HOST = os.environ.get("FREESWITCH_ESL_HOST", "127.0.0.1")
_ESL_PORT = int(os.environ.get("FREESWITCH_ESL_PORT", "8022"))
_ESL_PASSWORD = os.environ.get("FREESWITCH_ESL_PASSWORD", "ClueCon")
_SIP_PROXY_HOST = os.environ.get("SIP_PROXY_HOST", "192.168.0.116")
_SIP_PROXY_PORT = int(os.environ.get("SIP_PROXY_PORT", "5060"))


class OriginateError(Exception):
    """Raised for any ESL-level failure — connection refused, auth
    rejected, or the originate command itself rejected outright. Never
    raised for 'the contact didn't answer' — that's a real, expected
    outcome resolved later via the calls table (call_session_id ends up
    NULL / campaign_contacts.status becomes no_answer), not an error
    surfaced here. This module only reports whether FreeSWITCH ACCEPTED
    the command, exactly like EslClient::originate_async()'s own
    "Accepted, not succeeded" contract."""


async def _read_until_blank_line(reader: asyncio.StreamReader) -> dict[str, str]:
    """ESL inline-mode replies are a block of 'Header: value' lines
    terminated by a blank line — never a fixed-length read, since header
    block size varies per reply."""
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\n", b"\r\n"):
            break
        decoded = line.decode(errors="replace").rstrip("\r\n")
        if ":" in decoded:
            key, _, value = decoded.partition(":")
            headers[key.strip()] = value.strip()
    return headers


async def originate_call(phone_number: str, caller_id: str) -> str:
    """Commands FreeSWITCH to place a call to phone_number, routed into
    the dialplan as if it were an inbound call to caller_id (the agent's
    own DID) — see module docstring for why. Returns the FreeSWITCH Job-UUID
    for the background originate job (the actual answer/failure outcome
    arrives later as a FreeSWITCH event this module does not itself listen
    for — see worker.py's polling of the calls table instead). Raises
    OriginateError if FreeSWITCH rejected the command outright."""
    try:
        reader, writer = await asyncio.open_connection(_ESL_HOST, _ESL_PORT)
    except OSError as exc:
        raise OriginateError(f"cannot reach FreeSWITCH ESL at {_ESL_HOST}:{_ESL_PORT}: {exc}") from exc

    try:
        # Inline mode handshake: FreeSWITCH sends "Content-Type: auth/request"
        # unprompted on connect; reply with "auth <password>".
        await _read_until_blank_line(reader)
        writer.write(f"auth {_ESL_PASSWORD}\n\n".encode())
        await writer.drain()
        auth_reply = await _read_until_blank_line(reader)
        if auth_reply.get("Reply-Text", "").strip() != "+OK accepted":
            raise OriginateError(f"ESL auth rejected: {auth_reply}")

        dial_string = f"sofia/external/sip:{phone_number}@{_SIP_PROXY_HOST}:{_SIP_PROXY_PORT}"
        # Bridge to a FRESH leg addressed directly to the agent's DID
        # (caller_id), sent back out to Kamailio — not a local dialplan
        # extension execution. See module docstring for why: Kamailio's
        # own active routing only wires a DID into the real AI pipeline
        # when it sees that DID as the SIP Request-URI of an incoming
        # INVITE, which `&bridge(...)` produces and `<exten> XML
        # <context>` does not.
        bridge_target = f"sofia/external/sip:{caller_id}@{_SIP_PROXY_HOST}:{_SIP_PROXY_PORT}"
        command = (
            f"bgapi originate {{origination_caller_id_number={caller_id}}}"
            f"{dial_string} &bridge({bridge_target})"
        )
        writer.write(f"{command}\n\n".encode())
        await writer.drain()
        reply = await _read_until_blank_line(reader)

        reply_text = reply.get("Reply-Text", "")
        if not reply_text.startswith("+OK"):
            raise OriginateError(f"originate rejected: {reply}")

        # +OK Job-UUID: <uuid>
        job_uuid = reply_text.split("Job-UUID:")[-1].strip() if "Job-UUID:" in reply_text else ""
        log.info(
            "originate_call: accepted phone_number=%s caller_id=%s job_uuid=%s",
            phone_number, caller_id, job_uuid,
        )
        return job_uuid
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


class EslJobEventListener:
    """Persistent ESL connection subscribed to BACKGROUND_JOB events —
    mirrors the Gateway's own EslClient/EslEventListener split (gateway/
    include/telephony/EslEventListener.h): a command connection (see
    originate_call() above) is separate from a long-lived event
    connection, since a command connection isn't listening for
    asynchronous events at all.

    VERIFIED 2026-07-28 against a real local FreeSWITCH (`bgapi status` while
    subscribed to `event plain BACKGROUND_JOB`): the outer ESL envelope read
    by `_read_until_blank_line()` only ever has Content-Type/Content-Length —
    it does NOT carry Job-UUID. The envelope's `Content-Length` bytes are
    themselves a second header block (Event-Name, Job-UUID, a *second*
    nested Content-Length, ...) followed by a blank line and then the
    original bgapi command's own reply text (e.g. "+OK <channel-uuid>" on
    an answered call, "-ERR NO_ANSWER" / "-ERR USER_BUSY" on a failed one).
    _parse_job_event() below parses that inner block, not the outer one.
    """

    def __init__(self, on_job_complete) -> None:
        # on_job_complete(job_uuid: str, succeeded: bool, detail: str) -> Awaitable[None]
        self._on_job_complete = on_job_complete
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("EslJobEventListener: connection lost, reconnecting in 5s")
                await asyncio.sleep(5)

    async def _listen_once(self) -> None:
        reader, writer = await asyncio.open_connection(_ESL_HOST, _ESL_PORT)
        try:
            await _read_until_blank_line(reader)
            writer.write(f"auth {_ESL_PASSWORD}\n\n".encode())
            await writer.drain()
            await _read_until_blank_line(reader)

            writer.write(b"event plain BACKGROUND_JOB\n\n")
            await writer.drain()
            await _read_until_blank_line(reader)  # command/reply ack for the event subscription itself

            log.info("EslJobEventListener: subscribed to BACKGROUND_JOB events")
            while not self._stopped:
                headers = await _read_until_blank_line(reader)
                if not headers:
                    continue
                content_length = int(headers.get("Content-Length", "0"))
                body = (await reader.readexactly(content_length)).decode(errors="replace") if content_length else ""
                job_uuid, succeeded, detail = _parse_job_event(headers, body)
                if job_uuid:
                    await self._on_job_complete(job_uuid, succeeded, detail)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def _parse_job_event(headers: dict[str, str], body: str) -> tuple[str, bool, str]:
    """`headers` is the outer ESL envelope (Content-Type/Content-Length
    only — see EslJobEventListener's docstring on why Job-UUID is never
    there). `body` is itself a header block (Event-Name, Job-UUID, a
    second nested Content-Length, ...) followed by a blank line and the
    job's actual +OK/-ERR reply text; parse that inner block here."""
    del headers  # outer envelope carries nothing this needs — see docstring above
    header_part, sep, reply_part = body.partition("\r\n\r\n")
    if not sep:
        header_part, sep, reply_part = body.partition("\n\n")

    event_headers: dict[str, str] = {}
    for line in header_part.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            event_headers[key.strip()] = value.strip()

    job_uuid = event_headers.get("Job-UUID", "")
    reply_text = reply_part.strip()
    succeeded = reply_text.startswith("+OK")
    return job_uuid, succeeded, reply_text
