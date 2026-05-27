import asyncio
import json
import logging
import os
import ssl
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
    getattr(logger, level if level in {"info", "warning", "error"} else "info")(msg)
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
    await _log("info", f"Job started - room: {ctx.room.name}")
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
    await ctx.connect()

    if phone_number:
        trunk_id = os.getenv("TWILIO_TRUNK_SID", "") or os.getenv("OUTBOUND_TRUNK_ID", "")
        if not trunk_id:
            await _log("error", "TWILIO_TRUNK_SID not set - cannot place outbound call")
            ctx.shutdown()
            return
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
        except Exception as exc:
            await _log("error", f"SIP dial failed for {phone_number}: {exc}")
            ctx.shutdown()
            return

    session = _build_session(tool_ctx.build_tool_list(enabled_tools), system_prompt)
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO

        kwargs = {
            "room": ctx.room,
            "agent": OutboundAssistant(instructions=system_prompt),
            "room_options": _RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())),
        }
    else:
        kwargs = {
            "room": ctx.room,
            "agent": OutboundAssistant(instructions=system_prompt),
            "room_input_options": RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        }
    await session.start(**kwargs)

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

    active_model = os.getenv("GEMINI_MODEL", "")
    if "3.1" not in active_model and "2.5" not in active_model:
        try:
            await session.generate_reply(instructions=f"The call just connected. Greet the lead and ask if you're speaking with {lead_name}.")
        except Exception as exc:
            await _log("warning", f"generate_reply failed: {exc}")

    done = asyncio.Event()
    sip_identity = f"sip_{phone_number}" if phone_number else None

    def _on_participant_disconnected(participant: rtc.RemoteParticipant):
        if sip_identity and participant.identity == sip_identity:
            done.set()

    def _on_disconnected():
        done.set()

    ctx.room.on("participant_disconnected", _on_participant_disconnected)
    ctx.room.on("disconnected", _on_disconnected)

    # --- Silence watchdog: auto-end call if no activity for SILENCE_TIMEOUT_SECONDS ---
    SILENCE_TIMEOUT_SECONDS = float(os.getenv("CALL_SILENCE_TIMEOUT", "12"))
    silence_task: Optional[asyncio.Task] = None
    loop = asyncio.get_event_loop()

    async def _silence_watchdog():
        try:
            await asyncio.sleep(SILENCE_TIMEOUT_SECONDS)
            await _log("info", f"Silence watchdog fired after {SILENCE_TIMEOUT_SECONDS}s - ending call")
            try:
                from db import log_call as _log_call
                await _log_call(
                    phone_number=phone_number or "unknown",
                    lead_name=lead_name,
                    outcome="silence_timeout",
                    reason="no activity after final response",
                    duration_seconds=int(__import__("time").time() - tool_ctx._call_start_time),
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
        nonlocal silence_task
        if silence_task and not silence_task.done():
            silence_task.cancel()
        silence_task = loop.create_task(_silence_watchdog())

    for event_name in ("conversation_item_added", "agent_state_changed", "user_state_changed", "user_input_transcribed"):
        try:
            session.on(event_name, _reset_silence_timer)
        except Exception:
            pass
    _reset_silence_timer()
    try:
        await asyncio.wait_for(done.wait(), timeout=3600)
    except asyncio.TimeoutError:
        await _log("warning", "Call reached 1-hour safety timeout")
    await session.aclose()


if __name__ == "__main__":
    init_db()
    if validate_runtime_config():
        raise SystemExit(1)
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller"))
