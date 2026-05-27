import os
import uuid
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

DEFAULTS = {
    "LIVEKIT_URL": os.getenv("LIVEKIT_URL", ""),
    "LIVEKIT_API_KEY": os.getenv("LIVEKIT_API_KEY", ""),
    "LIVEKIT_API_SECRET": os.getenv("LIVEKIT_API_SECRET", ""),
    "GOOGLE_API_KEY": os.getenv("GOOGLE_API_KEY", ""),
    "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
    "GEMINI_TTS_VOICE": os.getenv("GEMINI_TTS_VOICE", "Aoede"),
    "USE_GEMINI_REALTIME": os.getenv("USE_GEMINI_REALTIME", "true"),
    "TWILIO_ACCOUNT_SID": os.getenv("TWILIO_ACCOUNT_SID", ""),
    "TWILIO_AUTH_TOKEN": os.getenv("TWILIO_AUTH_TOKEN", ""),
    "TWILIO_FROM_NUMBER": os.getenv("TWILIO_FROM_NUMBER", ""),
    "TWILIO_TRUNK_SID": os.getenv("TWILIO_TRUNK_SID", ""),
    "DEFAULT_TRANSFER_NUMBER": os.getenv("DEFAULT_TRANSFER_NUMBER", ""),
    "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
    "SUPABASE_SERVICE_KEY": os.getenv("SUPABASE_SERVICE_KEY", ""),
    "CALCOM_API_KEY": os.getenv("CALCOM_API_KEY", ""),
    "CALCOM_BOOKING_URL": os.getenv("CALCOM_BOOKING_URL", "https://cal.id/graviton/aicaller"),
    "CALCOM_TIMEZONE": os.getenv("CALCOM_TIMEZONE", "America/Chicago"),
    "DEEPGRAM_API_KEY": os.getenv("DEEPGRAM_API_KEY", ""),
}


def _default(key: str) -> str:
    return os.getenv(key, DEFAULTS.get(key, ""))


SUPABASE_URL = _default("SUPABASE_URL")
SUPABASE_KEY = _default("SUPABASE_SERVICE_KEY")

SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY", "TWILIO_AUTH_TOKEN", "SUPABASE_SERVICE_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY", "DEEPGRAM_API_KEY",
}

ENV_ALIASES = {
    "system_prompt": "SYSTEM_PROMPT",
    "ENABLED_TOOLS": "ENABLED_TOOLS",
}


def _sdb():
    from supabase import create_client
    return create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))


async def _adb():
    from supabase._async.client import create_client
    return await create_client(_default("SUPABASE_URL"), _default("SUPABASE_SERVICE_KEY"))


def init_db() -> None:
    url = os.getenv("SUPABASE_URL", SUPABASE_URL)
    key = os.getenv("SUPABASE_SERVICE_KEY", SUPABASE_KEY)
    if not url or not key:
        print("SUPABASE_URL or SUPABASE_SERVICE_KEY not set.")
        return
    try:
        db = _sdb()
        db.table("settings").select("key").limit(1).execute()
        print("Supabase connected")
    except Exception as exc:
        print(f"Supabase connection failed: {exc}")


async def get_all_settings() -> dict:
    KNOWN_KEYS = [
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE",
        "USE_GEMINI_REALTIME", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_TRUNK_SID",
        "DEFAULT_TRANSFER_NUMBER", "SUPABASE_URL", "OUTBOUND_TRUNK_ID", "DEEPGRAM_API_KEY", "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET", "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID",
        "CALCOM_BOOKING_URL", "CALCOM_TIMEZONE", "ENABLED_TOOLS", "SYSTEM_PROMPT",
    ]
    out = {}
    for k in KNOWN_KEYS:
        env_val = _default(k)
        out[k] = {"value": "" if k in SENSITIVE_KEYS else env_val, "configured": bool(env_val)}
    return out


async def save_settings(data: dict) -> None:
    raise RuntimeError("Runtime settings writes are disabled. Configure values via VPS environment variables.")


async def get_setting(key: str, default: str = "") -> str:
    env_key = ENV_ALIASES.get(key, key)
    return os.getenv(env_key, _default(env_key) or default)


async def set_setting(key: str, value: str) -> None:
    raise RuntimeError("Runtime settings writes are disabled. Configure values via VPS environment variables.")


async def get_enabled_tools() -> list:
    raw = os.getenv("ENABLED_TOOLS", "")
    if not raw:
        return []
    try:
        import json
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def validate_runtime_config() -> list[str]:
    required = [
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "GOOGLE_API_KEY",
        "TWILIO_TRUNK_SID",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "CALCOM_API_KEY",
        "CALCOM_BOOKING_URL",
        "CALCOM_TIMEZONE",
    ]
    missing = [key for key in required if not os.getenv(key, "").strip()]
    if missing:
        print("Missing required environment variables: " + ", ".join(missing))
    else:
        print("Runtime environment validation passed")
    return missing


async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        db = await _adb()
        await db.table("error_logs").insert({
            "id": str(uuid.uuid4()), "source": source, "level": level, "message": message[:500],
            "detail": detail[:2000], "timestamp": datetime.now().isoformat(),
        }).execute()
    except Exception:
        pass


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    db = await _adb()
    q = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
    if level:
        q = q.eq("level", level)
    if source:
        q = q.eq("source", source)
    return (await q.execute()).data or []


async def clear_errors() -> None:
    db = await _adb()
    await db.table("error_logs").delete().neq("id", "").execute()


async def insert_appointment(
    name: str,
    phone: str,
    date: str,
    time: str,
    service: str,
    calcom_booking_uid: Optional[str] = None,
    timezone: str = "America/Chicago",
    synced_to_calcom: bool = False,
    booking_source: str = "calcom",
    status: str = "booked",
) -> str:
    full_id = str(uuid.uuid4())
    db = await _adb()
    row = {
        "id": full_id,
        "name": name,
        "phone": phone,
        "date": date,
        "time": time,
        "service": service,
        "status": status,
        "created_at": datetime.now().isoformat(),
        "timezone": timezone,
        "synced_to_calcom": 1 if synced_to_calcom else 0,
        "booking_source": booking_source,
    }
    if calcom_booking_uid:
        row["calcom_booking_uid"] = calcom_booking_uid
    await db.table("appointments").insert(row).execute()
    return full_id[:8].upper()


async def check_slot(date: str, time: str) -> bool:
    db = await _adb()
    result = await db.table("appointments").select("id").eq("date", date).eq("time", time).eq("status", "booked").maybe_single().execute()
    return result.data is None


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18 and await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
            return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    db = await _adb()
    q = db.table("appointments").select("*").order("date").order("time")
    if date_filter:
        q = q.eq("date", date_filter)
    return (await q.execute()).data or []


async def get_appointment(appointment_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("appointments").select("*").eq("id", appointment_id).maybe_single().execute()
    return result.data if result else None


async def cancel_appointment(appointment_id: str) -> bool:
    db = await _adb()
    result = await db.table("appointments").update({"status": "cancelled"}).eq("id", appointment_id).eq("status", "booked").execute()
    return len(result.data or []) > 0


async def get_appointments_by_phone(phone: str) -> list:
    db = await _adb()
    return (await db.table("appointments").select("*").eq("phone", phone).order("date", desc=True).execute()).data or []


async def log_call(phone_number: str, lead_name: Optional[str], outcome: str, reason: str, duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None) -> None:
    db = await _adb()
    row = {
        "id": str(uuid.uuid4()), "phone_number": phone_number, "lead_name": lead_name, "outcome": outcome,
        "reason": reason, "duration_seconds": duration_seconds, "timestamp": datetime.now().isoformat(),
    }
    if recording_url:
        row["recording_url"] = recording_url
    if notes:
        row["notes"] = notes
    await db.table("call_logs").insert(row).execute()


async def get_all_calls(page: int = 1, limit: int = 20) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    return (await db.table("call_logs").select("*").order("timestamp", desc=True).range(offset, offset + limit - 1).execute()).data or []


async def get_calls_by_phone(phone: str) -> list:
    db = await _adb()
    return (await db.table("call_logs").select("*").eq("phone_number", phone).order("timestamp", desc=True).execute()).data or []


async def update_call_notes(call_id: str, notes: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"notes": notes}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_contacts() -> list:
    rows = await get_all_calls(page=1, limit=2000)
    contacts = {}
    for row in rows:
        phone = row["phone_number"]
        if phone not in contacts:
            contacts[phone] = {"phone_number": phone, "lead_name": row.get("lead_name"), "total_calls": 0, "booked": 0, "last_call": row["timestamp"], "last_outcome": row.get("outcome")}
        contacts[phone]["total_calls"] += 1
        if row.get("outcome") == "booked":
            contacts[phone]["booked"] += 1
    return sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)


async def get_stats() -> dict:
    db = await _adb()
    rows = (await db.table("call_logs").select("outcome, duration_seconds, timestamp").execute()).data or []
    total_calls = len(rows)
    booked = sum(1 for r in rows if r.get("outcome") == "booked")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    durations = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    avg_dur = sum(durations) / len(durations) if durations else 0
    outcomes = {}
    for r in rows:
        o = r.get("outcome") or "unknown"
        outcomes[o] = outcomes.get(o, 0) + 1
    daily = defaultdict(int)
    dur_sum = defaultdict(float)
    dur_cnt = defaultdict(int)
    for r in rows:
        ts = (r.get("timestamp") or "")[:10]
        if ts:
            daily[ts] += 1
        outcome = r.get("outcome") or "unknown"
        if r.get("duration_seconds"):
            dur_sum[outcome] += r["duration_seconds"]
            dur_cnt[outcome] += 1
    today = datetime.now().date()
    timeline = [{"date": (today - timedelta(days=i)).isoformat(), "count": daily.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]
    duration_by_outcome = {outcome: round(dur_sum[outcome] / dur_cnt[outcome], 1) for outcome in dur_sum if dur_cnt[outcome]}
    return {
        "total_calls": total_calls,
        "booked": booked,
        "not_interested": not_interested,
        "avg_duration_seconds": round(avg_dur, 1),
        "booking_rate_percent": round((booked / total_calls * 100) if total_calls else 0, 1),
        "outcomes": outcomes,
        "timeline": timeline,
        "duration_by_outcome": duration_by_outcome,
    }


async def create_campaign(name: str, contacts_json: str, schedule_type: str = "once", schedule_time: str = "09:00", call_delay_seconds: int = 3, system_prompt: Optional[str] = None, agent_profile_id: Optional[str] = None) -> str:
    campaign_id = str(uuid.uuid4())
    db = await _adb()
    row = {
        "id": campaign_id, "name": name, "status": "active", "contacts_json": contacts_json,
        "schedule_type": schedule_type, "schedule_time": schedule_time, "call_delay_seconds": call_delay_seconds,
        "created_at": datetime.now().isoformat(), "total_dispatched": 0, "total_failed": 0,
    }
    if system_prompt:
        row["system_prompt"] = system_prompt
    if agent_profile_id:
        row["agent_profile_id"] = agent_profile_id
    await db.table("campaigns").insert(row).execute()
    return campaign_id


async def get_all_campaigns() -> list:
    db = await _adb()
    return (await db.table("campaigns").select("*").order("created_at", desc=True).execute()).data or []


async def get_campaign(campaign_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    return result.data if result else None


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
    return len(result.data or []) > 0


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    db = await _adb()
    await db.table("campaigns").update({"last_run_at": datetime.now().isoformat(), "total_dispatched": dispatched, "total_failed": failed, "status": "completed"}).eq("id", campaign_id).execute()


async def delete_campaign(campaign_id: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
    return len(result.data or []) > 0


async def add_contact_memory(phone: str, insight: str) -> None:
    db = await _adb()
    await db.table("contact_memory").insert({"id": str(uuid.uuid4()), "phone_number": phone, "insight": insight[:1000], "created_at": datetime.now().isoformat()}).execute()


async def get_contact_memory(phone: str) -> list:
    db = await _adb()
    return (await db.table("contact_memory").select("insight, created_at").eq("phone_number", phone).order("created_at", desc=True).limit(20).execute()).data or []


async def compress_contact_memory(phone: str, compressed: str) -> None:
    db = await _adb()
    await db.table("contact_memory").delete().eq("phone_number", phone).execute()
    await db.table("contact_memory").insert({"id": str(uuid.uuid4()), "phone_number": phone, "insight": compressed[:2000], "created_at": datetime.now().isoformat()}).execute()


async def get_all_agent_profiles() -> list:
    db = await _adb()
    return (await db.table("agent_profiles").select("*").order("created_at").execute()).data or []


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
    return result.data if result else None


async def create_agent_profile(name: str, voice: str = "Aoede", model: str = "gemini-3.1-flash-live-preview", system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False) -> str:
    profile_id = str(uuid.uuid4())
    db = await _adb()
    if is_default:
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").insert({
        "id": profile_id, "name": name, "voice": voice, "model": model, "system_prompt": system_prompt,
        "enabled_tools": enabled_tools, "is_default": 1 if is_default else 0, "created_at": datetime.now().isoformat(),
    }).execute()
    return profile_id


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").update(updates).eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def delete_agent_profile(profile_id: str) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def set_default_agent_profile(profile_id: str) -> None:
    db = await _adb()
    await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").update({"is_default": 1}).eq("id", profile_id).execute()
