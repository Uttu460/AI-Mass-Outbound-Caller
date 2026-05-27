import asyncio
import json
import logging
import os
import ssl
import time
import traceback
from typing import Optional

import certifi

_orig_ssl = ssl.create_default_context


def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)


ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions

try:
    from livekit.agents import RoomOptions as _RoomOptions

    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False

from livekit.plugins import noise_cancellation, silero

from db import get_enabled_tools, init_db, log_error, validate_runtime_config
from prompts import build_prompt
from tools import AppointmentTools
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")


async def _log(level: str, msg: str, detail: str = "") -> None:
    getattr(logger, level if level in {"info", "warning", "error"} else "info")(
        f"{msg}" + (f" | {detail}" if detail else "")
    )
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None
_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    from livekit.plugins import google as _gp

    _deepgram_stt = _dg.STT
    _google_realtime = getattr(getattr(_gp, "realtime", None), "RealtimeModel", None)
    _google_beta_realtime = getattr(getattr(getattr(_gp, "beta", None), "realtime", None), "RealtimeModel", None)
    _google_llm = getattr(_gp, "LLM", None)
    _google_tts = getattr(_gp, "TTS", None)
except ImportError:
    logger.warning("Google or Deepgram plugins not installed")


def _build_session(tools: list, system_prompt: str) -> AgentSession:
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"
    realtime_class = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and realtime_class is not None:
        try:
            from google.genai import types as _gt

            realtime_kwargs = {
                "model": gemini_model,
                "voice": gemini_voice,
                "instructions": system_prompt,
                "realtime_input_config": _gt.RealtimeInputConfig(
                    automatic_activity_detection=_gt.AutomaticActivityDetection(
                        end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                        silence_duration_ms=2000,
                        prefix_padding_ms=200,
                    )
                ),
                "session_resumption": _gt.SessionResumptionConfig(transparent=True),
                "context_window_compression": _gt.ContextWindowCompressionConfig(
                    trigger_tokens=25600,
                    sliding_window=_gt.SlidingWindow(target_tokens=12800),
                ),
            }
        except Exception:
            realtime_kwargs = {"model": gemini_model, "voice": gemini_voice, "instructions": system_prompt}
        return AgentSession(llm=realtime_class(**realtime_kwargs), tools=tools)

    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    return AgentSession(stt=stt, llm=_google_llm(model="gemini-2.0-flash"), tts=tts, vad=silero.VAD.load(), tools=tools)


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


async def entrypoint(ctx: agents.JobContext) -> None:
    await _log("info", f"[STARTUP] Worker entrypoint invoked - room: {ctx.room.name}")
    await _log("info", f"[STARTUP] LiveKit URL: {os.getenv('LIVEKIT_URL', '<unset>')}")
    await _log("info", f"[STARTUP] GEMINI_MODEL: {os.getenv('GEMINI_MODEL', '<unset>')}")
    await _log("info", f"[STARTUP] USE_GEMINI_REALTIME: {os.getenv('USE_GEMINI_REALTIME', 'true')}")
    phone_number: Optional[str] = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    custom_prompt: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override: Optional[str] = None

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
            lead_name = data.get("lead_name", lead_name)
            business_name = data.get("business_name", business_name)
            service_type = data.get("service_type", service_type)
            custom_prompt = data.get("system_prompt")
            voice_override = data.get("voice_override")
            model_override = data.get("model_override")
            tools_override = data.get("tools_override")
        except Exception:
            await _log("warning", "Invalid JSON in job metadata")

    system_prompt = build_prompt(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        phone=phone_number or "",
        custom_prompt=custom_prompt,
    )
    if voice_override:
        os.environ["GEMINI_TTS_VOICE"] = voice_override
    if model_override:
        os.environ["GEMINI_MODEL"] = model_override

    enabled_tools = await get_enabled_tools()
    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            pass

    tool_ctx = AppointmentTools(ctx, phone_number, lead_name, business_name)
    try:
        await ctx.connect()
        await _log("info", f"[ROOM] Connected to LiveKit room: {ctx.room.name}")
    except Exception as exc:
        await _log("error", "[ROOM] ctx.connect() FAILED", traceback.format_exc())
        raise

    # Track SIP participant + audio track readiness via asyncio events
    sip_identity = f"sip_{phone_number}" if phone_number else None
    sip_participant_joined = asyncio.Event()
    sip_audio_subscribed = asyncio.Event()

    def _on_participant_connected(p: rtc.RemoteParticipant):
        logger.info(f"[ROOM] Participant connected: {p.identity} (kind={p.kind})")
        if sip_identity and p.identity == sip_identity:
            sip_participant_joined.set()

    def _on_track_subscribed(track, publication, p: rtc.RemoteParticipant):
        logger.info(f"[ROOM] Track subscribed from {p.identity}: kind={track.kind} sid={publication.sid}")
        if sip_identity and p.identity == sip_identity and track.kind == rtc.TrackKind.KIND_AUDIO:
            sip_audio_subscribed.set()

    ctx.room.on("participant_connected", _on_participant_connected)
    ctx.room.on("track_subscribed", _on_track_subscribed)

    # ===== BUILD + START THE AGENT SESSION FIRST (before SIP dial) =====
    try:
        session = _build_session(tool_ctx.build_tool_list(enabled_tools), system_prompt)
        await _log("info", "[SESSION] AgentSession built successfully")
    except Exception as exc:
        await _log("error", "[SESSION] Failed to build AgentSession", traceback.format_exc())
        raise

    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO

        session_kwargs = {
            "room": ctx.room,
            "agent": OutboundAssistant(instructions=system_prompt),
            "room_options": _RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())),
        }
    else:
        session_kwargs = {
            "room": ctx.room,
            "agent": OutboundAssistant(instructions=system_prompt),
            "room_input_options": RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        }
    try:
        await _log("info", "[SESSION] Calling session.start()...")
        await session.start(**session_kwargs)
        await _log("info", "[SESSION] session.start() returned")
    except Exception as exc:
        await _log("error", "[SESSION] session.start() FAILED", traceback.format_exc())
        raise

    # ===== WAIT UNTIL THE SESSION IS ACTUALLY RUNNING =====
    async def _wait_session_ready(timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Probe a few common readiness attributes across livekit-agents versions
            for attr in ("is_running", "_started", "_running", "started"):
                val = getattr(session, attr, None)
                if isinstance(val, bool) and val:
                    return True
                if hasattr(val, "is_set") and val.is_set():
                    return True
            # No reliable attribute? Just give it a tick.
            await asyncio.sleep(0.1)
            # If no detectable readiness flag exists, assume ready after one tick
            if not any(hasattr(session, a) for a in ("is_running", "_started", "_running", "started")):
                return True
        return False

    ready = await _wait_session_ready(timeout=10.0)
    if ready:
        await _log("info", "[SESSION] AgentSession is RUNNING and ready for input")
    else:
        await _log("warning", "[SESSION] Could not confirm AgentSession readiness within 10s - continuing anyway")

    # ===== NOW DIAL THE OUTBOUND SIP CALL =====
    if phone_number:
        trunk_id = os.getenv("TWILIO_TRUNK_SID", "") or os.getenv("OUTBOUND_TRUNK_ID", "")
        if not trunk_id:
            await _log("error", "[SIP] TWILIO_TRUNK_SID not set - cannot place outbound call")
            ctx.shutdown()
            return
        try:
            await _log("info", f"[SIP] Dialing {phone_number} via trunk {trunk_id}")
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=sip_identity,
                    wait_until_answered=True,
                )
            )
            await _log("info", f"[SIP] Call answered by {phone_number}")
        except Exception as exc:
            await _log("error", f"[SIP] Dial failed for {phone_number}: {exc}", traceback.format_exc())
            ctx.shutdown()
            return

        # Wait for the SIP participant to actually join the room
        try:
            await asyncio.wait_for(sip_participant_joined.wait(), timeout=10.0)
            await _log("info", f"[ROOM] SIP participant {sip_identity} confirmed in room")
        except asyncio.TimeoutError:
            await _log("warning", f"[ROOM] SIP participant {sip_identity} did not appear within 10s - continuing")

        # Wait for the SIP audio track subscription (the actual audio pipeline)
        try:
            await asyncio.wait_for(sip_audio_subscribed.wait(), timeout=10.0)
            await _log("info", "[ROOM] SIP audio track subscribed - audio pipeline active")
        except asyncio.TimeoutError:
            await _log("warning", "[ROOM] SIP audio track not subscribed within 10s - continuing")

    if phone_number:
        aws_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
        aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        aws_bucket = os.getenv("S3_BUCKET", "")
        s3_endpoint = os.getenv("S3_ENDPOINT_URL", "")
        s3_region = os.getenv("S3_REGION", "ap-northeast-1")
        if aws_key and aws_secret and aws_bucket:
            try:
                recording_path = f"recordings/{ctx.room.name}.ogg"
                await ctx.api.egress.start_room_composite_egress(
                    api.RoomCompositeEgressRequest(
                        room_name=ctx.room.name,
                        audio_only=True,
                        file_outputs=[
                            api.EncodedFileOutput(
                                file_type=api.EncodedFileType.OGG,
                                filepath=recording_path,
                                s3=api.S3Upload(
                                    access_key=aws_key,
                                    secret=aws_secret,
                                    bucket=aws_bucket,
                                    region=s3_region,
                                    endpoint=s3_endpoint,
                                ),
                            )
                        ],
                    )
                )
                tool_ctx.recording_url = f"{s3_endpoint.rstrip('/')}/{aws_bucket}/{recording_path}" if s3_endpoint else f"s3://{aws_bucket}/{recording_path}"
            except Exception as exc:
                await _log("warning", f"Recording start failed: {exc}")

    # Always trigger first reply so the AI speaks first.
    # Realtime models like gemini-3.1-flash-live-preview also need this kick-off,
    # otherwise they sit silent waiting for user audio.
    greet_instructions = (
        f"The call just connected. Speak first immediately - greet the lead "
        f"and ask if you're speaking with someone from {business_name}."
    )
    max_attempts = 5
    backoff = 0.5
    for attempt in range(1, max_attempts + 1):
        try:
            await _log("info", f"[SESSION] generate_reply() attempt {attempt}/{max_attempts}")
            await session.generate_reply(instructions=greet_instructions)
            await _log("info", "[SESSION] Initial generate_reply() returned successfully")
            break
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "isn't running" in msg or "not running" in msg or "not started" in msg:
                await _log("warning", f"[SESSION] Session not ready yet (attempt {attempt}): {exc}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            await _log("error", f"[SESSION] generate_reply RuntimeError: {exc}", traceback.format_exc())
            break
        except Exception as exc:
            await _log("warning", f"[SESSION] generate_reply failed (attempt {attempt}): {exc}", traceback.format_exc())
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 4.0)
    else:
        await _log("error", f"[SESSION] generate_reply() never succeeded after {max_attempts} attempts")

    done = asyncio.Event()

    def _on_participant_disconnected(participant: rtc.RemoteParticipant):
        if sip_identity and participant.identity == sip_identity:
            done.set()

    def _on_disconnected():
        done.set()

    ctx.room.on("participant_disconnected", _on_participant_disconnected)
    ctx.room.on("disconnected", _on_disconnected)

    # --- Silence watchdog: auto-end call after inactivity, but ONLY after agent has spoken at least once ---
    SILENCE_TIMEOUT_SECONDS = float(os.getenv("CALL_SILENCE_TIMEOUT", "15"))
    AGENT_FIRST_SPEECH_TIMEOUT = float(os.getenv("AGENT_FIRST_SPEECH_TIMEOUT", "30"))
    silence_task: Optional[asyncio.Task] = None
    agent_has_spoken = False
    loop = asyncio.get_event_loop()

    async def _silence_watchdog(timeout: float, reason: str):
        try:
            await asyncio.sleep(timeout)
            await _log("info", f"[WATCHDOG] Fired after {timeout}s ({reason}) - ending call")
            try:
                from db import log_call as _log_call
                await _log_call(
                    phone_number=phone_number or "unknown",
                    lead_name=lead_name,
                    outcome="silence_timeout",
                    reason=reason,
                    duration_seconds=int(time.time() - tool_ctx._call_start_time),
                    recording_url=tool_ctx.recording_url,
                )
            except Exception:
                pass
            try:
                await ctx.room.disconnect()
            except Exception:
                pass
            done.set()
        except asyncio.CancelledError:
            return

    def _reset_silence_timer(*_args, **_kwargs):
        nonlocal silence_task, agent_has_spoken
        # Detect agent speech via state changes
        for arg in _args:
            arg_str = str(arg).lower()
            if "agent" in arg_str and ("speaking" in arg_str or "thinking" in arg_str):
                agent_has_spoken = True
        if silence_task and not silence_task.done():
            silence_task.cancel()
        if agent_has_spoken:
            silence_task = loop.create_task(_silence_watchdog(SILENCE_TIMEOUT_SECONDS, "no activity after agent response"))

    def _on_conv_item(*args, **kwargs):
        nonlocal agent_has_spoken
        agent_has_spoken = True
        logger.info(f"[CONV] conversation_item_added args={args!r} kwargs={kwargs!r}")
        _reset_silence_timer(*args, **kwargs)

    def _on_agent_state(*args, **kwargs):
        logger.info(f"[STATE] agent_state_changed args={args!r}")
        _reset_silence_timer(*args, **kwargs)

    def _on_user_state(*args, **kwargs):
        logger.info(f"[STATE] user_state_changed args={args!r}")
        _reset_silence_timer(*args, **kwargs)

    def _on_user_transcript(*args, **kwargs):
        logger.info(f"[STT] user_input_transcribed args={args!r}")
        _reset_silence_timer(*args, **kwargs)

    handlers = {
        "conversation_item_added": _on_conv_item,
        "agent_state_changed": _on_agent_state,
        "user_state_changed": _on_user_state,
        "user_input_transcribed": _on_user_transcript,
    }
    for event_name, handler in handlers.items():
        try:
            session.on(event_name, handler)
            logger.info(f"[SESSION] Subscribed to event: {event_name}")
        except Exception as exc:
            logger.warning(f"[SESSION] Could not subscribe to {event_name}: {exc}")

    # Initial grace period: give the agent up to AGENT_FIRST_SPEECH_TIMEOUT seconds to speak first.
    silence_task = loop.create_task(_silence_watchdog(AGENT_FIRST_SPEECH_TIMEOUT, "agent never spoke first"))
    try:
        await asyncio.wait_for(done.wait(), timeout=3600)
    except asyncio.TimeoutError:
        await _log("warning", "Call reached 1-hour safety timeout")
    await session.aclose()


if __name__ == "__main__":
    print("=" * 60, flush=True)
    print("[BOOT] OutboundAI LiveKit Agent Worker starting...", flush=True)
    print(f"[BOOT] LIVEKIT_URL={os.getenv('LIVEKIT_URL', '<unset>')}", flush=True)
    print(f"[BOOT] GEMINI_MODEL={os.getenv('GEMINI_MODEL', '<unset>')}", flush=True)
    print(f"[BOOT] USE_GEMINI_REALTIME={os.getenv('USE_GEMINI_REALTIME', 'true')}", flush=True)
    print("=" * 60, flush=True)

    # Verbose logging for diagnostics
    logging.getLogger("livekit").setLevel(logging.INFO)
    logging.getLogger("livekit.agents").setLevel(logging.INFO)
    logging.getLogger("livekit.plugins.google").setLevel(logging.INFO)

    init_db()
    if validate_runtime_config():
        print("[BOOT] FATAL: missing required environment variables - aborting worker", flush=True)
        raise SystemExit(1)

    print("[BOOT] Registering worker with LiveKit (agent_name=outbound-caller)...", flush=True)
    try:
        agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller"))
    except Exception:
        print("[BOOT] Worker crashed with exception:", flush=True)
        traceback.print_exc()
        raise
