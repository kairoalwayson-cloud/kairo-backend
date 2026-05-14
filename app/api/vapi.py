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


def _parse_appointment_dt(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse ISO date + HH:MM time into a naive datetime."""
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except Exception:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            return None


# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_book_appointment(args: dict, db: Session, business: Business) -> str:
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

    appointment_dt = _parse_appointment_dt(date_str, time_str)

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

    db.commit()
    db.refresh(lead)

    logger.info(f"Appointment booked: lead={lead.id} at={appointment_dt}")

    # Create Google Calendar event
    try:
        if appointment_dt:
            from app.api.calendar import create_appointment_event
            event_link = create_appointment_event(
                user_id=str(business.owner_id),
                lead_name=lead_name or "Lead",
                service=service or "Service",
                location=location or "",
                start_dt=appointment_dt,
                db=db,
            )
            if event_link:
                logger.info(f"Calendar event created: {event_link}")
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

    # Format confirmation sentence Ana will speak
    if appointment_dt:
        formatted = appointment_dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
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


# ── Main webhook ──────────────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "book_appointment":    handle_book_appointment,
    "check_availability":  handle_check_availability,
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

        # Try to match an existing lead by phone
        lead_id = None
        if caller_phone:
            from app.models.lead import Lead as LeadModel
            lead = db.query(LeadModel).filter(
                LeadModel.business_id == business.id,
                LeadModel.phone == caller_phone,
            ).first()
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
                result_text = handler(args, db, business)
            except Exception as e:
                logger.error(f"Tool handler error ({fn_name}): {e}")
                result_text = "I'm sorry, I had trouble booking that. Let me transfer you to a team member."
        else:
            result_text = "Function not available right now."

        results.append({"toolCallId": tc_id, "result": result_text})

    return {"results": results}
