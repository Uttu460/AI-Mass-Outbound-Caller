import asyncio
import json
import logging
import os
import random
import ssl
from pathlib import Path
from typing import Optional

import aiohttp
import certifi
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

_orig_ssl = ssl.create_default_context


def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)


ssl.create_default_context = _certifi_ssl

from db import (
    cancel_appointment,
    clear_errors,
    create_agent_profile,
    create_campaign,
    delete_agent_profile,
    delete_campaign,
    get_agent_profile,
    get_all_agent_profiles,
    get_all_appointments,
    get_all_calls,
    get_all_campaigns,
    get_all_settings,
    get_appointment,
    get_calls_by_phone,
    get_campaign,
    get_contacts,
    get_logs,
    get_setting,
    get_stats,
    init_db,
    log_error,
    save_settings,
    set_default_agent_profile,
    set_setting,
    update_agent_profile,
    update_call_notes,
    update_campaign_run_stats,
    update_campaign_status,
)
from prompts import DEFAULT_SYSTEM_PROMPT

CALCOM_BOOKING_VERSION = "2024-08-13"
CALCOM_CANCEL_VERSION = "2026-02-25"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")
init_db()

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None

app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")


class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class StatusRequest(BaseModel):
    status: str


@app.on_event("startup")
async def _startup():
    if _scheduler:
        _scheduler.start()
        await _reschedule_all_campaigns()


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    return await get_setting(key, "")


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "ui" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8")) if html_path.exists() else HTMLResponse("Dashboard not found", status_code=404)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "OutboundAI", "version": "1.0.0"}


async def _dispatch_call(payload: dict):
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured.")
    from livekit import api as lk_api

    room_name = f"call-{payload['phone_number'].replace('+', '')}-{random.randint(1000, 9999)}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
    lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
    try:
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(payload))
        )
        await log_error("server", f"Call dispatched to {payload['phone_number']}", f"room={room_name}", "info")
        return {"status": "dispatched", "room": room_name, "phone": payload["phone_number"]}
    finally:
        await lk.aclose()
        await session.close()


@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must be in E.164 format: +919876543210")
    effective_prompt = req.system_prompt
    effective_voice = None
    effective_model = None
    effective_tools = None
    if req.agent_profile_id:
        profile = await get_agent_profile(req.agent_profile_id)
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")
    if not effective_prompt:
        effective_prompt = await get_setting("system_prompt", "") or None
    metadata = {
        "phone_number": phone,
        "lead_name": req.lead_name,
        "business_name": req.business_name,
        "service_type": req.service_type,
        "system_prompt": effective_prompt,
    }
    if effective_voice:
        metadata["voice_override"] = effective_voice
    if effective_model:
        metadata["model_override"] = effective_model
    if effective_tools:
        metadata["tools_override"] = effective_tools
    return await _dispatch_call(metadata)


@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    if not await update_call_notes(call_id, req.notes):
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    appointment = await get_appointment(appointment_id)
    if appointment and appointment.get("calcom_booking_uid"):
        api_key = await eff("CALCOM_API_KEY")
        if api_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post(
                        f"https://api.cal.com/v2/bookings/{appointment['calcom_booking_uid']}/cancel",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "cal-api-version": CALCOM_CANCEL_VERSION,
                            "Content-Type": "application/json",
                        },
                        json={"cancellationReason": "Cancelled from OutboundAI dashboard"},
                    )
            except Exception as exc:
                await log_error("server", "Cal.com cancellation failed", str(exc), "warning")
    if not await cancel_appointment(appointment_id):
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    raise HTTPException(409, "Prompt is env-managed. Set SYSTEM_PROMPT on the VPS/container environment.")


@app.delete("/api/prompt")
async def api_reset_prompt():
    raise HTTPException(409, "Prompt is env-managed. Remove SYSTEM_PROMPT from the VPS/container environment.")


@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    raise HTTPException(409, "Settings are env-managed. Update them in your VPS/container environment and redeploy.")


@app.post("/api/setup/trunk")
async def api_setup_trunk():
    trunk_sid = await eff("TWILIO_TRUNK_SID")
    if not trunk_sid:
        raise HTTPException(400, "Set TWILIO_TRUNK_SID on the VPS/container environment first.")
    return {"status": "linked", "trunk_id": trunk_sid}


@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    return await get_all_agent_profiles()


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    profile_id = await create_agent_profile(
        name=req.name,
        voice=req.voice,
        model=req.model,
        system_prompt=req.system_prompt,
        enabled_tools=req.enabled_tools,
        is_default=req.is_default,
    )
    return {"status": "created", "id": profile_id}


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(
        profile_id,
        {
            "name": req.name,
            "voice": req.voice,
            "model": req.model,
            "system_prompt": req.system_prompt,
            "enabled_tools": req.enabled_tools,
            "is_default": 1 if req.is_default else 0,
        },
    )
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    if not await delete_agent_profile(profile_id):
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    await set_default_agent_profile(profile_id)
    return {"status": "default set"}


async def _dispatch_one(lk, lk_api, contact: dict, prompt: Optional[str], profile: Optional[dict] = None) -> bool:
    try:
        metadata = {
            "phone_number": contact["phone"],
            "lead_name": contact.get("lead_name", "there"),
            "business_name": contact.get("business_name", "our company"),
            "service_type": contact.get("service_type", "our service"),
            "system_prompt": prompt or (await get_setting("system_prompt", "")) or None,
        }
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):
                metadata["voice_override"] = profile["voice"]
            if profile.get("model"):
                metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"):
                metadata["tools_override"] = profile["enabled_tools"]
        room_name = f"camp-{contact['phone'].replace('+', '')}-{random.randint(100, 999)}"
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata))
        )
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    profile = await get_agent_profile(campaign["agent_profile_id"]) if campaign.get("agent_profile_id") else None
    from livekit import api as lk_api

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
    lk = lk_api.LiveKitAPI(url=await eff("LIVEKIT_URL"), api_key=await eff("LIVEKIT_API_KEY"), api_secret=await eff("LIVEKIT_API_SECRET"), session=session)
    ok_count = 0
    fail_count = 0
    try:
        for idx, contact in enumerate(contacts):
            if contact.get("phone", "").startswith("+") and await _dispatch_one(lk, lk_api, contact, campaign.get("system_prompt"), profile):
                ok_count += 1
            else:
                fail_count += 1
            if idx < len(contacts) - 1:
                await asyncio.sleep(int(campaign.get("call_delay_seconds") or 3))
    finally:
        await update_campaign_run_stats(campaign_id, ok_count, fail_count)
        await lk.aclose()
        await session.close()


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    for campaign in await get_all_campaigns():
        if campaign.get("status") == "active" and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign["id"], campaign["schedule_type"], campaign.get("schedule_time", "09:00"))


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except Exception:
        hour, minute = 9, 0
    trigger = CronTrigger(hour=hour, minute=minute) if schedule_type == "daily" else CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    campaign_id = await create_campaign(
        name=req.name,
        contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type,
        schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds,
        system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)
    return {"status": "created", "campaign_id": campaign_id, "campaign": await get_campaign(campaign_id)}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    if not await delete_campaign(campaign_id):
        raise HTTPException(404, "Campaign not found")
    if _scheduler and _scheduler.get_job(f"campaign_{campaign_id}"):
        _scheduler.remove_job(f"campaign_{campaign_id}")
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    if not await get_campaign(campaign_id):
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if not await update_campaign_status(campaign_id, req.status):
        raise HTTPException(404, "Campaign not found")
    if req.status == "paused" and _scheduler and _scheduler.get_job(f"campaign_{campaign_id}"):
        _scheduler.remove_job(f"campaign_{campaign_id}")
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}
