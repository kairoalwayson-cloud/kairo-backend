"""
Conversation Service
Orchestrates the full message flow:
  1. Find or create Lead
  2. Load conversation history
  3. Build system prompt from business config
  4. Call Claude
  5. Apply collected info to Lead
  6. Save messages to DB
  7. Return response
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.business import Business
from app.models.lead import Lead
from app.models.conversation import Conversation, Message
from app.models.service import Service
from app.models.service_zone import ServiceZone
from app.models.escalation_rule import EscalationRule
from app.models.channel import Channel
from app.services.prompt_engine import generate_system_prompt
from app.services.ai_engine import call_claude, detect_language

logger = logging.getLogger(__name__)


def _get_or_create_lead(
    db: Session,
    business_id,
    channel: str,
    identifier: str,  # phone number or email from the contact
) -> Lead:
    """Find existing lead or create a new one."""
    lead = (
        db.query(Lead)
        .filter(
            Lead.business_id == business_id,
            Lead.channel == channel,
            Lead.phone == identifier if channel != "email" else Lead.email == identifier,
        )
        .first()
    )
    if not lead:
        lead = Lead(
            business_id=business_id,
            channel=channel,
            phone=identifier if channel != "email" else None,
            email=identifier if channel == "email" else None,
            status="new",
            last_message_at=datetime.now(timezone.utc),
        )
        db.add(lead)
        db.flush()  # get the id without committing
    return lead


def _get_or_create_conversation(db: Session, lead: Lead, channel: str) -> Conversation:
    """Get the active conversation or create one."""
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.lead_id == lead.id,
            Conversation.status == "active",
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if not conv:
        conv = Conversation(lead_id=lead.id, channel=channel, status="active")
        db.add(conv)
        db.flush()
    return conv


def _load_history(db: Session, conversation_id) -> list[dict]:
    """Load conversation messages in Claude format."""
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return [
        {"role": m.role, "content": m.content}
        for m in messages
        if m.role in ("user", "assistant")
    ]


def _update_lead_from_info(lead: Lead, collected: dict) -> None:
    """Apply any newly collected info to the lead record."""
    if collected.get("name") and not lead.name:
        lead.name = collected["name"]
    if collected.get("phone") and not lead.phone:
        lead.phone = collected["phone"]
    if collected.get("email") and not lead.email:
        lead.email = collected["email"]
    if collected.get("service") and not lead.service_requested:
        lead.service_requested = collected["service"]
    if collected.get("location") and not lead.location:
        lead.location = collected["location"]


def process_message(
    db: Session,
    business_id,
    channel: str,       # whatsapp / sms / email / voice
    sender_id: str,     # phone number or email of the person sending
    text: str,          # message content
) -> dict:
    """
    Main entry point for processing an incoming message.
    Returns: {response_text, lead_id, conversation_id, escalate, schedule_request}
    """
    # 1. Load business config
    business = db.query(Business).filter(Business.id == business_id, Business.is_active == True).first()
    if not business:
        return {"response_text": "Business not found.", "lead_id": None, "conversation_id": None}

    services = db.query(Service).filter(Service.business_id == business_id, Service.active == True).all()
    zones = db.query(ServiceZone).filter(ServiceZone.business_id == business_id).all()
    escalation_rules = db.query(EscalationRule).filter(EscalationRule.business_id == business_id).all()

    # Check if Google Calendar is connected
    cal_channel = db.query(Channel).filter(
        Channel.business_id == business_id,
        Channel.type == "google_calendar",
        Channel.active == True,
    ).first()
    calendar_connected = cal_channel is not None

    # 2. Build system prompt
    system_prompt = generate_system_prompt(
        business=business,
        services=services,
        service_zones=zones,
        escalation_rules=escalation_rules,
        calendar_connected=calendar_connected,
    )

    # 3. Find or create lead + conversation
    lead = _get_or_create_lead(db, business_id, channel, sender_id)
    conversation = _get_or_create_conversation(db, lead, channel)

    # 4. Detect language and update lead if not set
    if not lead.language or lead.language == "pt":
        detected = detect_language(text)
        if detected != "pt":
            lead.language = detected

    # 5. Load history
    history = _load_history(db, conversation.id)

    # 6. Call Claude
    try:
        ai_response = call_claude(
            system_prompt=system_prompt,
            history=history,
            user_message=text,
        )
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        fallback = "Sorry, I'm having technical difficulties. Please try again in a moment."
        db.commit()
        return {
            "response_text": fallback,
            "lead_id": str(lead.id),
            "conversation_id": str(conversation.id),
            "escalate": None,
            "schedule_request": None,
        }

    # 7. Apply collected info to lead
    if ai_response.collected_info:
        _update_lead_from_info(lead, ai_response.collected_info)

    # 8. Update lead status
    lead.last_message_at = datetime.now(timezone.utc)
    if ai_response.should_escalate:
        lead.status = "waiting_human"
        conversation.status = "waiting_human"
    elif ai_response.has_schedule_request:
        lead.status = "scheduled"

    if lead.status == "new" and ai_response.collected_info:
        lead.status = "qualified"

    # 9. Save messages to DB
    user_msg = Message(
        conversation_id=conversation.id,
        role="user",
        content=text,
    )
    assistant_msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=ai_response.message,
    )
    db.add(user_msg)
    db.add(assistant_msg)
    db.commit()

    return {
        "response_text": ai_response.message,
        "lead_id": str(lead.id),
        "conversation_id": str(conversation.id),
        "escalate": ai_response.escalate,
        "schedule_request": ai_response.schedule_request,
        "tokens_used": ai_response.tokens_used,
    }
