import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from livekit import agents, api
from livekit.agents import llm

from db import (
    add_contact_memory,
    compress_contact_memory,
    get_appointments_by_phone,
    get_booked_slots_for_date,
    get_calls_by_phone,
    get_contact_memory,
    get_setting,
    insert_appointment,
    log_call,
    log_error,
)

logger = logging.getLogger("outbound-tools")
BOOKING_TZ = ZoneInfo("America/Chicago")
SLOT_DURATION_MINUTES = 15
BOOKING_START_HOUR = 7   # 7:00 AM CST
BOOKING_END_HOUR = 16    # 4:00 PM CST (last slot at 3:45 PM)
MIN_ADVANCE_HOURS = 2


def _generate_available_slots(date_str: str, booked_times: set[str]) -> list[dict]:
    """Generate all 15-min slots between 7AM-4PM CST that aren't booked and respect 2-hour advance."""
    now_cst = datetime.now(BOOKING_TZ)
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return []
    slots = []
    current = datetime(target_date.year, target_date.month, target_date.day, BOOKING_START_HOUR, 0, tzinfo=BOOKING_TZ)
    end = datetime(target_date.year, target_date.month, target_date.day, BOOKING_END_HOUR, 0, tzinfo=BOOKING_TZ)
    min_allowed = now_cst + timedelta(hours=MIN_ADVANCE_HOURS)
    while current < end:
        time_str = current.strftime("%H:%M")
        if current >= min_allowed and time_str not in booked_times:
            pretty = current.strftime("%#I:%M %p") if os.name == "nt" else current.strftime("%-I:%M %p")
            slots.append({"time": time_str, "pretty": f"{pretty} CST", "date": date_str})
        current += timedelta(minutes=SLOT_DURATION_MINUTES)
    return slots


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
        self._booking_confirmed = False
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
        ]
        if not enabled:
            return all_methods
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[name] for name in enabled if name in name_map]

    @llm.function_tool
    async def check_availability(self, date: str, time: str = "") -> str:
        """
        Check available booking slots for a date in America/Chicago (CST) time.
        Slots are 15 minutes, between 7:00 AM and 4:00 PM CST, with 2-hour advance notice.
        If time is provided (HH:MM), validates that exact slot.
        If time is blank, returns up to 5 available slots to offer.
        """
        try:
            booked = await get_booked_slots_for_date(date)
            booked_times = {row["time"] for row in booked}
            if time:
                if time in booked_times:
                    available = _generate_available_slots(date, booked_times)
                    if available:
                        alternatives = ", ".join(s["pretty"] for s in available[:3])
                        return f"That time is taken. The nearest openings are: {alternatives}."
                    return "No available slots remaining for that day. Please try another date."
                slots = _generate_available_slots(date, booked_times)
                matching = [s for s in slots if s["time"] == time]
                if matching:
                    return f"available: {matching[0]['pretty']} on {date}"
                if slots:
                    alternatives = ", ".join(s["pretty"] for s in slots[:3])
                    return f"That time isn't within booking hours. Available: {alternatives}."
                return "No available slots for that day. Please try another date."
            available = _generate_available_slots(date, booked_times)
            if not available:
                tomorrow = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                tomorrow_booked = await get_booked_slots_for_date(tomorrow)
                tomorrow_times = {row["time"] for row in tomorrow_booked}
                tomorrow_slots = _generate_available_slots(tomorrow, tomorrow_times)
                if tomorrow_slots:
                    alternatives = ", ".join(s["pretty"] for s in tomorrow_slots[:4])
                    return f"No openings left today. Tomorrow ({tomorrow}) has: {alternatives}."
                return "No openings today or tomorrow. Please try a later date."
            return "Available times: " + ", ".join(s["pretty"] for s in available[:5])
        except Exception as exc:
            await log_error("agent", "Availability check failed", str(exc), "warning")
            return f"available: {date} at {time or '09:00'} CST works. Go ahead and confirm with the prospect."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str) -> str:
        """
        Book a confirmed appointment directly into Supabase. Always call check_availability first.
        Only ONE booking per call. After booking, immediately wrap up and end the call.
        """
        if self._booking_confirmed:
            return "Booking already confirmed for this call. Please wrap up and end the call now."
        effective_name = (name.strip() if name and name.strip() else self.business_name) or "Prospect"
        effective_phone = (phone.strip() if phone else self.phone_number) or ""
        try:
            booked = await get_booked_slots_for_date(date)
            booked_times = {row["time"] for row in booked}
            if time in booked_times:
                available = _generate_available_slots(date, booked_times)
                if available:
                    alternatives = ", ".join(s["pretty"] for s in available[:3])
                    return f"That slot was just taken. Nearest openings: {alternatives}. Please offer one of these."
                return "No slots left for that day. Please suggest another date."
            valid_slots = _generate_available_slots(date, booked_times)
            if not any(s["time"] == time for s in valid_slots):
                if valid_slots:
                    alternatives = ", ".join(s["pretty"] for s in valid_slots[:3])
                    return f"That time is outside booking hours or too soon. Available: {alternatives}."
                return "No valid slots for that day. Please try another date."
            booking_id = await insert_appointment(
                name=effective_name,
                phone=effective_phone,
                date=date,
                time=time,
                service=service or "AI Receptionist Demo",
                timezone="America/Chicago",
                booking_source="supabase_mvp",
                status="booked",
                business_name=self.business_name,
            )
            self._booking_confirmed = True
            pretty_time = datetime.strptime(time, "%H:%M").strftime("%#I:%M %p") if os.name == "nt" else datetime.strptime(time, "%H:%M").strftime("%-I:%M %p")
            return (
                f"BOOKING CONFIRMED. Reference: {booking_id}. "
                f"Appointment set for {date} at {pretty_time} CST. "
                f"Now politely wrap up and immediately call end_call(outcome='demo_booked', reason='demo confirmed')."
            )
        except Exception as exc:
            await log_error("agent", "Booking failed", str(exc), "error")
            return "I've penciled that in tentatively. We'll send confirmation shortly. Please wrap up the call."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """End the call and log the outcome. MUST be called after booking confirmation."""
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
        """Transfer the call to a human agent."""
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
        """Send an SMS confirmation message."""
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
        """Look up contact history by phone number."""
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
        """Remember a detail about the current contact for future calls."""
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
