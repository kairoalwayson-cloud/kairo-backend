"""
Vapi.ai webhook and tool-call endpoints.
Vapi calls these during a voice conversation when the assistant invokes a Tool.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime
import logging
import uuid

from app.database import get_db
from app.models.lead import Lead
from app.models.conversation import Conversation
from app.models.business import Business
from app.models.channel import Channel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/vapi", tags=["vapi"])


# ── Pydantic schemas ────────────────────────────────────────────────────────

class ToolCallMessage(BaseModel):
    type: str                         # "tool-calls"
    toolCallList: Optional[list] = None


class VapiRequest(BaseModel):
    message: dict


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_business(db: Session, message: dict) -> Optional[Business]:
    """
    Find business from a Vapi message using the most reliable identifiers in order:
    1. assistantId → matches vapi_assistant_id on Business (most reliable)
    2. Phone number → Channel table lookup
    3. Fallback → first active business (single-tenant pilots)
    """
    call = message.get("call", {})

    # 1. Try by Vapi assistant ID
    assistant_id = (
        call.get("assistantId")
        or call.get("assistant", {}).get("id", "")
    )
    if assistant_id:
        biz = db.query(Business).filter(Business.vapi_assistant_id == assistant_id).first()
        if biz:
            return biz

    # 2. Try by phone number via Channel table
    phone_number = (
        call.get("phoneNumber", {}).get("number", "")
        or call.get("customer", {}).get("number", "")
    )
    if phone_number:
        clean = phone_number.replace("whatsapp:", "").strip()
        channel = db.query(Channel).filter(
            Channel.identifier == clean,
            Channel.provider.in_(["twilio", "vapi"]),
            Channel.active == True,
        ).first()
        if channel:
            return db.query(Business).filter(Business.id == channel.business_id).first()

    # 3. Fallback: first active business
    return db.query(Business).filter(Business.is_active == True).first()


def _parse_appointment_dt(date_str: str, time_str: str, tz_name: str = "America/New_York") -> Optional[datetime]:
    """Parse ISO date + HH:MM 24h time into a timezone-aware UTC datetime."""
    try:
        import pytz
        naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        tz = pytz.timezone(tz_name)
        local_dt = tz.localize(naive)
        return local_dt.astimezone(pytz.utc).replace(tzinfo=None)  # store as UTC naive for DB
    except Exception:
        try:
            return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            try:
                return datetime.strptime(date_str, "%Y-%m-%d")
            except Exception:
                return None


# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_book_appointment(args: dict, db: Session, business: Business, vapi_call_id: str = None) -> str:
    """
    Creates (or updates) a Lead record with a confirmed appointment.
    Returns a short sentence Ana speaks back to the caller.
    """
    lead_name  = args.get("lead_name", "").strip()
    phone      = args.get("phone", "").strip()
    service    = args.get("service", "").strip()
    location   = args.get("location", "").strip()
    date_str   = args.get("date", "")
    time_str   = args.get("time", "08:00")
    notes      = args.get("notes", "")

    # Guard: require name AND phone before booking
    if not phone:
        return "I still need the caller's phone number before I can book the appointment. Could you please provide it?"
    if not lead_name:
        return "I still need the caller's name before I can book the appointment. Could you please provide it?"

    appointment_dt = _parse_appointment_dt(date_str, time_str, business.timezone or "America/New_York")

    # Upsert lead by phone within this business
    lead = None
    if phone:
        lead = db.query(Lead).filter(
            Lead.business_id == business.id,
            Lead.phone == phone,
        ).first()

    if not lead:
        lead = Lead(
            id=uuid.uuid4(),
            business_id=business.id,
            channel="voice",
        )
        db.add(lead)

    lead.name              = lead_name or lead.name
    lead.phone             = phone or lead.phone
    lead.service_requested = service or lead.service_requested
    lead.location          = location or lead.location
    lead.status            = "scheduled"
    lead.appointment_at    = appointment_dt
    if notes:
        lead.notes = notes
    lead.last_message_at = datetime.utcnow()
    if vapi_call_id:
        lead.vapi_call_id = vapi_call_id

    db.commit()
    db.refresh(lead)

    logger.info(f"Appointment booked: lead={lead.id} at={appointment_dt}")

    # Create Google Calendar event and save ID on the lead
    try:
        if appointment_dt:
            from app.api.calendar import create_appointment_event
            event_id = create_appointment_event(
                user_id=str(business.owner_id),
                lead_name=lead_name or "Lead",
                service=service or "Service",
                location=location or "",
                start_dt=appointment_dt,
                db=db,
            )
            if event_id:
                lead.google_event_id = event_id
                db.commit()
                logger.info(f"Calendar event created: {event_id}")
    except Exception as e:
        logger.error(f"Calendar event creation error: {e}")

    # Send detailed appointment report via WhatsApp
    try:
        if business.notify_on_new_lead:
            from app.services.notify_service import send_appointment_report
            from app.models.conversation import Conversation, Message
            convs = db.query(Conversation).filter(Conversation.lead_id == lead.id).all()
            msgs = []
            for conv in convs:
                for m in db.query(Message).filter(Message.conversation_id == conv.id).order_by(Message.created_at).all():
                    msgs.append({"role": m.role, "content": m.content})
            send_appointment_report(business, lead, appointment_dt, msgs)
    except Exception as e:
        logger.error(f"Booking report error: {e}")

    # Format confirmation using original local date/time (appointment_dt is UTC)
    if appointment_dt:
        try:
            local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            hour = local_dt.hour
            minute = local_dt.minute
            suffix = "AM" if hour < 12 else "PM"
            h12 = hour % 12 or 12
            time_label = f"{h12}:{minute:02d} {suffix}"
            formatted = f"{local_dt.strftime('%B')} {local_dt.day} at {time_label}"
        except Exception:
            formatted = f"{date_str} at {time_str}"
        return (
            f"Perfect, {lead_name or 'your appointment'} is confirmed for {formatted}. "
            f"We look forward to seeing you! Is there anything else I can help you with?"
        )
    return (
        f"Your appointment has been confirmed, {lead_name or 'thank you'}! "
        f"We will see you soon. Is there anything else I can help you with?"
    )


def handle_check_availability(args: dict, db: Session, business: Business) -> str:
    """Check real availability against the owner's Google Calendar."""
    date_str = args.get("date", "")
    time_str = args.get("time", "")
    try:
        from app.api.calendar import is_slot_available
        available = is_slot_available(str(business.owner_id), date_str, time_str, db)
    except Exception as e:
        logger.error(f"Availability check error: {e}")
        available = True

    if available:
        return f"Yes, {date_str} at {time_str} is available. Would you like me to confirm that appointment for you?"
    else:
        return f"I'm sorry, {date_str} at {time_str} is already booked. Could you suggest another date or time?"


def handle_get_available_slots(args: dict, db: Session, business: Business) -> str:
    """Return available time slots for a given date from the owner's calendar."""
    from datetime import timedelta
    date_str = args.get("date", "")
    current_time_str = args.get("current_time", "")  # HH:MM 24h, passed by Maya for today

    try:
        from app.api.calendar import get_day_availability
        slots = get_day_availability(str(business.owner_id), date_str, db)
    except Exception as e:
        logger.error(f"get_available_slots error: {e}")
        return "We have openings throughout the day. What time works best for you?"

    if not slots:
        return "Unfortunately we have no openings on that day. Could you suggest another date?"

    # Build cutoff: skip slots at or before (current_time + 1h) when caller gives current time
    cutoff_dt = None
    if current_time_str:
        try:
            base = datetime.strptime(date_str, "%Y-%m-%d")
            t = datetime.strptime(current_time_str.strip(), "%H:%M")
            cutoff_dt = base.replace(hour=t.hour, minute=t.minute) + timedelta(hours=1)
        except Exception:
            pass

    # Extract hourly marks within each free window (max 4 options)
    available_times: list[str] = []
    for slot in slots:
        start = datetime.fromisoformat(slot.start)
        end = datetime.fromisoformat(slot.end)
        current = start.replace(minute=0, second=0)
        if current < start:
            current += timedelta(hours=1)
        while current < end and len(available_times) < 4:
            if cutoff_dt and current <= cutoff_dt:
                current += timedelta(hours=1)
                continue
            hour = current.hour
            suffix = "AM" if hour < 12 else "PM"
            h12 = hour % 12 or 12
            available_times.append(f"{h12}:00 {suffix}")
            current += timedelta(hours=1)

    if not available_times:
        return "We have no more openings for today. Would you like to schedule for tomorrow or another day?"

    if len(available_times) == 1:
        return f"We have one opening on that day: {available_times[0]}. Does that work for you?"

    slots_text = ", ".join(available_times[:-1]) + f" or {available_times[-1]}"
    return f"We have openings at {slots_text} on that day. Which time works best for you?"


# ── Main webhook ──────────────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "book_appointment":       handle_book_appointment,
    "check_availability":     handle_check_availability,
    "get_available_slots":    handle_get_available_slots,
}


def _save_call_log(db: Session, message: dict) -> None:
    """Persist an end-of-call-report from Vapi as a CallLog record."""
    try:
        from app.models.call_log import CallLog
        from datetime import timezone

        call = message.get("call", {})
        business = _find_business(db, message)
        if not business:
            logger.warning("end-of-call-report: no business found, skipping save")
            return

        caller_raw = (
            call.get("customer", {}).get("number", "")
            or call.get("phoneNumber", {}).get("number", "")
        )
        caller_phone = caller_raw.strip() if caller_raw else None

        # Match lead: 1) by vapi_call_id, 2) by phone exact, 3) by phone fuzzy
        lead_id = None
        vapi_cid = call.get("id")
        from app.models.lead import Lead as LeadModel
        lead = None
        if vapi_cid:
            lead = db.query(LeadModel).filter(
                LeadModel.business_id == business.id,
                LeadModel.vapi_call_id == vapi_cid,
            ).first()
        if not lead and caller_phone:
            lead = db.query(LeadModel).filter(
                LeadModel.business_id == business.id,
                LeadModel.phone == caller_phone,
            ).order_by(LeadModel.created_at.desc()).first()
            if not lead:
                digits = "".join(c for c in caller_phone if c.isdigit())
                norm = digits[-10:] if len(digits) >= 10 else digits
                if norm:
                    candidates = db.query(LeadModel).filter(
                        LeadModel.business_id == business.id,
                        LeadModel.phone.isnot(None),
                    ).order_by(LeadModel.created_at.desc()).limit(200).all()
                    for cand in candidates:
                        d = "".join(c for c in cand.phone if c.isdigit())
                        if (d[-10:] if len(d) >= 10 else d) == norm:
                            lead = cand
                            break
        if lead:
            lead_id = lead.id

        def _parse_dt(s: str | None):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                return None

        record = CallLog(
            business_id=business.id,
            lead_id=lead_id,
            vapi_call_id=call.get("id"),
            caller_phone=caller_phone,
            duration_seconds=int(message.get("durationSeconds", 0)) or None,
            status="completed",
            transcript=message.get("transcript"),
            messages_json=message.get("messages"),
            summary=message.get("summary"),
            recording_url=message.get("recordingUrl"),
            started_at=_parse_dt(call.get("startedAt")),
            ended_at=_parse_dt(call.get("endedAt")),
        )
        db.add(record)
        db.commit()
        logger.info(f"CallLog saved: business={business.id} phone={caller_phone} duration={record.duration_seconds}s")
    except Exception as e:
        logger.error(f"Failed to save call log: {e}")


@router.post("/tool-calls")
async def vapi_tool_calls(request: Request, db: Session = Depends(get_db)):
    """
    Vapi sends a POST here each time the assistant invokes a Tool during a call.
    We must respond with { results: [ { toolCallId, result } ] }
    """
    body = await request.json()
    message = body.get("message", {})
    msg_type = message.get("type", "")

    if msg_type == "end-of-call-report":
        _save_call_log(db, message)
        return {"received": True}

    if msg_type != "tool-calls":
        return {"received": True}

    business = _find_business(db, message)
    vapi_call_id = message.get("call", {}).get("id")  # pass to book_appointment

    results = []
    for tool_call in message.get("toolCallList", []):
        tc_id   = tool_call.get("id")
        fn_name = tool_call.get("function", {}).get("name", "")
        args    = tool_call.get("function", {}).get("arguments", {})
        if isinstance(args, str):
            import json
            try:
                args = json.loads(args)
            except Exception:
                args = {}

        handler = TOOL_HANDLERS.get(fn_name)
        if handler and business:
            try:
                if fn_name == "book_appointment":
                    result_text = handler(args, db, business, vapi_call_id=vapi_call_id)
                else:
                    result_text = handler(args, db, business)
            except Exception as e:
                logger.error(f"Tool handler error ({fn_name}): {e}")
                result_text = "I'm sorry, I had trouble booking that. Let me transfer you to a team member."
        else:
            result_text = "Function not available right now."

        results.append({"toolCallId": tc_id, "result": result_text})

    return {"results": results}
