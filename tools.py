import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from livekit import agents, api
from livekit.agents import llm

from db import (
    add_contact_memory,
    check_slot,
    compress_contact_memory,
    get_appointments_by_phone,
    get_calls_by_phone,
    get_contact_memory,
    get_next_available,
    get_setting,
    insert_appointment,
    log_call,
    log_error,
)

logger = logging.getLogger("outbound-tools")
CALCOM_EVENT_VERSION = "2024-06-14"
CALCOM_BOOKING_VERSION = "2024-08-13"
CALCOM_CANCEL_VERSION = "2026-02-25"
CALCOM_TZ = ZoneInfo("America/Chicago")


def _fallback_email(phone: str, business_name: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit()) or "lead"
    safe_biz = "".join(ch for ch in (business_name or "lead").lower() if ch.isalnum())[:24] or "lead"
    return f"{safe_biz}-{digits}@outboundai.local"


class AppointmentTools(llm.ToolContext):
    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_name: Optional[str] = None,
        business_name: Optional[str] = None,
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.business_name = business_name or "our company"
        self._call_start_time = time.time()
        self.recording_url: Optional[str] = None
        self._cal_event_cache: Optional[dict] = None
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list) -> list:
        all_methods = [
            self.check_availability,
            self.book_appointment,
            self.end_call,
            self.transfer_to_human,
            self.send_sms_confirmation,
            self.lookup_contact,
            self.remember_details,
            self.book_calcom,
            self.cancel_calcom,
        ]
        if not enabled:
            return all_methods
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[name] for name in enabled if name in name_map]

    async def _effective_cal_settings(self) -> dict:
        booking_url = await get_setting("CALCOM_BOOKING_URL", "")
        timezone_name = await get_setting("CALCOM_TIMEZONE", "America/Chicago")
        api_key = await get_setting("CALCOM_API_KEY", "")
        return {
            "api_key": api_key or os.getenv("CALCOM_API_KEY", ""),
            "booking_url": booking_url or os.getenv("CALCOM_BOOKING_URL", "https://cal.id/graviton/aicaller"),
            "timezone": timezone_name or "America/Chicago",
        }

    async def _get_calcom_event(self) -> dict:
        if self._cal_event_cache:
            return self._cal_event_cache
        settings = await self._effective_cal_settings()
        booking_url = settings["booking_url"].rstrip("/")
        parts = booking_url.split("/")
        if len(parts) < 2:
            raise ValueError("CALCOM_BOOKING_URL is invalid")
        username = parts[-2]
        event_slug = parts[-1]
        api_key = settings["api_key"]
        if not api_key:
            raise ValueError("CALCOM_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.cal.com/v2/event-types",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "cal-api-version": CALCOM_EVENT_VERSION,
                },
                params={"username": username, "eventSlug": event_slug},
            )
        data = resp.json()
        if resp.status_code != 200 or not data.get("data"):
            raise ValueError(f"Failed to resolve Cal.com event type: HTTP {resp.status_code}")
        event = data["data"][0]
        self._cal_event_cache = {
            "id": event.get("id"),
            "slug": event_slug,
            "username": username,
            "length": event.get("lengthInMinutes") or 15,
            "location": (event.get("locations") or [{}])[0],
            "timezone": settings["timezone"],
        }
        return self._cal_event_cache

    async def _fetch_real_slots(self, date: str, days: int = 3) -> list[dict]:
        event = await self._get_calcom_event()
        settings = await self._effective_cal_settings()
        start_local = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(settings["timezone"]))
        end_local = (start_local + timedelta(days=max(days, 1))).replace(hour=23, minute=59, second=0, microsecond=0)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.cal.com/v1/slots",
                params={
                    "apiKey": settings["api_key"],
                    "usernameList": event["username"],
                    "eventTypeSlug": event["slug"],
                    "startTime": start_local.isoformat(),
                    "endTime": end_local.isoformat(),
                    "timeZone": settings["timezone"],
                },
            )
        data = resp.json()
        if resp.status_code != 200:
            raise ValueError(f"Cal.com slots lookup failed: HTTP {resp.status_code}")
        slots = []
        for slot_date, items in (data.get("slots") or {}).items():
            for item in items:
                raw = item.get("time")
                if not raw:
                    continue
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ZoneInfo(settings["timezone"]))
                slots.append(
                    {
                        "date": dt.strftime("%Y-%m-%d"),
                        "time": dt.strftime("%H:%M"),
                        "pretty": dt.strftime("%A %b %d at %-I:%M %p CST") if os.name != "nt" else dt.strftime("%A %b %d at %#I:%M %p CST"),
                        "iso_utc": dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                )
        return slots

    async def _fallback_local_booking(self, name: str, phone: str, date: str, time_text: str, service: str) -> str:
        booking_id = await insert_appointment(
            name=name,
            phone=phone,
            date=date,
            time=time_text,
            service=service,
            timezone="America/Chicago",
            synced_to_calcom=False,
        )
        return f"Calendar sync is temporarily unavailable, but I saved your booking request as {booking_id} for {date} at {time_text} CST."

    @llm.function_tool
    async def check_availability(self, date: str, time: str = "") -> str:
        """
        Check Cal.com availability for a date in America/Chicago time.
        If time is provided (HH:MM), validates that exact slot and returns nearby real options if unavailable.
        If time is blank, returns 2-5 real available slots to offer.
        """
        try:
            slots = await self._fetch_real_slots(date=date, days=3)
            same_day = [slot for slot in slots if slot["date"] == date]
            if not time:
                if not same_day:
                    if slots:
                        alternatives = ", ".join(slot["pretty"] for slot in slots[:3])
                        return f"No open times on {date}. The next real openings are {alternatives}."
                    return "No Cal.com slots are available right now."
                return "Available CST slots: " + ", ".join(slot["pretty"] for slot in same_day[:5])
            for slot in same_day:
                if slot["time"] == time:
                    return f"available: {slot['pretty']}"
            alternatives = ", ".join(slot["pretty"] for slot in same_day[:3]) if same_day else ""
            return f"unavailable: {alternatives or 'no open slots for that date'}"
        except Exception as exc:
            await log_error("agent", "Cal.com availability lookup failed", str(exc), "warning")
            try:
                if await check_slot(date, time or "09:00"):
                    return "available via CRM fallback"
                return f"unavailable: next available slot is {await get_next_available(date, time or '09:00')}"
            except Exception:
                return "Unable to check availability right now."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str) -> str:
        """
        Book the selected appointment. Primary flow is Cal.com first, then Supabase CRM backup.
        Always call check_availability before confirming a slot.
        """
        try:
            return await self.book_calcom(
                name=name,
                email="",
                date=date,
                start_time=time,
                notes=service,
                business_name=name or self.business_name,
                phone=phone,
            )
        except Exception as exc:
            await log_error("agent", "Booking flow crashed", str(exc), "error")
            return await self._fallback_local_booking(name, phone, date, time, service)

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        duration = int(time.time() - self._call_start_time)
        try:
            await log_call(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name,
                outcome=outcome,
                reason=reason,
                duration_seconds=duration,
                recording_url=self.recording_url,
            )
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call ended."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "Transfer unavailable: no fallback number configured."
        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for participant in self.ctx.room.remote_participants.values():
                participant_identity = participant.identity
                break
        if not participant_identity:
            return "Transfer failed: could not identify caller."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=f"tel:{destination.replace('tel:', '').replace('sip:', '')}",
                    play_dialtone=False,
                )
            )
            return "Transferring you to a human agent now. Please hold."
        except Exception:
            return "Transfer failed. Please call us back directly."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: Twilio not configured."
        try:
            from twilio.rest import Client

            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=phone))
            return f"SMS sent to {phone}."
        except Exception:
            return "SMS delivery failed, but booking is confirmed."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        try:
            calls = await get_calls_by_phone(phone)
            appointments = await get_appointments_by_phone(phone)
            memories = await get_contact_memory(phone)
            if not calls and not appointments and not memories:
                return f"No history for {phone}. First-time contact."
            lines = [f"Contact history for {phone}:"]
            if memories:
                lines.append("REMEMBERED:")
                lines.extend([f"- {m['insight']}" for m in memories[:10]])
            if calls:
                lines.append("CALL HISTORY:")
                lines.extend([f"- {(c.get('timestamp') or '')[:16]} - {c.get('outcome', '?')}: {c.get('reason', '')}" for c in calls[:5]])
            if appointments:
                lines.append("APPOINTMENTS:")
                lines.extend(
                    [
                        f"- {a.get('date')} {a.get('time')} {a.get('timezone', 'CST')} - {a.get('service')} [{a.get('status')}]"
                        for a in appointments[:3]
                    ]
                )
            return "\n".join(lines)
        except Exception:
            return "Unable to retrieve contact history."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        if not self.phone_number:
            return "Cannot remember - no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Remembered: {insight}"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            import google.generativeai as genai

            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key:
                return
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: model.generate_content(
                    "Compress these notes about a sales contact into 3-5 concise bullets. Keep all key facts.\n\n" + bullet_list
                ),
            )
            if response.text.strip():
                await compress_contact_memory(self.phone_number, response.text.strip())
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def book_calcom(
        self,
        name: str,
        email: str,
        date: str,
        start_time: str,
        notes: str = "",
        business_name: str = "",
        phone: str = "",
        custom_answers_json: str = "{}",
    ) -> str:
        """
        Book a confirmed slot in Cal.com using America/Chicago time, then mirror it into Supabase CRM.
        Email is optional and will be auto-generated if not available.
        """
        lead_phone = phone or self.phone_number or ""
        biz_name = business_name or self.business_name or name or "our company"
        timezone_name = "America/Chicago"
        try:
            matching = [slot for slot in await self._fetch_real_slots(date=date, days=1) if slot["date"] == date and slot["time"] == start_time]
            if not matching:
                return f"That slot is no longer open in Cal.com. Please offer one of these real CST times instead: {await self.check_availability(date, '')}"
            slot = matching[0]
            event = await self._get_calcom_event()
            answers = json.loads(custom_answers_json or "{}")
            attendee_email = email.strip() if email and email.strip() else _fallback_email(lead_phone, biz_name)
            payload = {
                "start": slot["iso_utc"],
                "attendee": {
                    "name": name or biz_name,
                    "timeZone": timezone_name,
                    "phoneNumber": lead_phone,
                    "language": "en",
                    "email": attendee_email,
                },
                "eventTypeId": event["id"],
                "eventTypeSlug": event["slug"],
                "username": event["username"],
                "bookingFieldsResponses": answers,
                "location": event["location"],
                "metadata": {
                    "source": "OutboundAI",
                    "business_name": biz_name,
                    "phone_number": lead_phone,
                    "scheduled_date": date,
                    "scheduled_time": start_time,
                },
                "lengthInMinutes": event["length"],
            }
            if notes:
                payload["metadata"]["notes"] = notes
            settings = await self._effective_cal_settings()
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.cal.com/v2/bookings",
                    headers={
                        "Authorization": f"Bearer {settings['api_key']}",
                        "cal-api-version": CALCOM_BOOKING_VERSION,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or data.get("error") or f"HTTP {resp.status_code}")
            booking = data.get("data") or {}
            booking_uid = booking.get("uid") or booking.get("bookingUid") or ""
            booking_id = await insert_appointment(
                name=biz_name,
                phone=lead_phone,
                date=date,
                time=start_time,
                service=notes or "AI Receptionist Demo",
                calcom_booking_uid=booking_uid,
                timezone=timezone_name,
                synced_to_calcom=True,
            )
            return (
                f"Confirmed in Cal.com. Booking ID: {booking_id}. "
                f"Calendar UID: {booking_uid or 'created'}. You're set for {date} at {start_time} CST."
            )
        except Exception as exc:
            await log_error("agent", "Cal.com booking failed - using Supabase fallback", str(exc), "error")
            return await self._fallback_local_booking(biz_name, lead_phone, date, start_time, notes or "AI Receptionist Demo")

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str = "") -> str:
        settings = await self._effective_cal_settings()
        if not settings["api_key"]:
            return "Cal.com not configured."
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"https://api.cal.com/v2/bookings/{booking_uid}/cancel",
                    headers={
                        "Authorization": f"Bearer {settings['api_key']}",
                        "cal-api-version": CALCOM_CANCEL_VERSION,
                        "Content-Type": "application/json",
                    },
                    json={"cancellationReason": reason or "Cancelled from OutboundAI"},
                )
            if resp.status_code not in (200, 201):
                raise ValueError(f"HTTP {resp.status_code}")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            return f"Cancellation failed: {exc}"
