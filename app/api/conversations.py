"""
Conversations API
Endpoints used by the dashboard and for testing the AI directly.
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.business import Business
from app.models.lead import Lead
from app.models.conversation import Conversation, Message
from app.services.conversation_service import process_message

router = APIRouter(prefix="/conversations", tags=["conversations"])


class SendMessageRequest(BaseModel):
    business_id: str
    channel: str = "web"          # web / whatsapp / sms / email
    sender_id: str                 # phone or email of the lead
    text: str


class SendMessageResponse(BaseModel):
    response_text: str
    lead_id: Optional[str]
    conversation_id: Optional[str]
    escalate: Optional[dict]
    schedule_request: Optional[dict]
    tokens_used: Optional[int]


@router.post("/message", response_model=SendMessageResponse)
def send_message(
    payload: SendMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a message through the AI engine (used for dashboard testing)."""
    # Verify the business belongs to this user
    business = db.query(Business).filter(
        Business.id == payload.business_id,
        Business.owner_id == current_user.id,
    ).first()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    result = process_message(
        db=db,
        business_id=business.id,
        channel=payload.channel,
        sender_id=payload.sender_id,
        text=payload.text,
    )
    return result


@router.get("/leads")
def list_leads(
    business_id: str,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List leads for a business."""
    business = db.query(Business).filter(
        Business.id == business_id,
        Business.owner_id == current_user.id,
    ).first()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    query = db.query(Lead).filter(Lead.business_id == business.id)
    if status:
        query = query.filter(Lead.status == status)

    from sqlalchemy import func
    leads = query.order_by(
        func.coalesce(Lead.last_message_at, Lead.created_at).desc()
    ).limit(100).all()

    return [
        {
            "id": str(l.id),
            "name": l.name,
            "phone": l.phone,
            "email": l.email,
            "service_requested": l.service_requested,
            "location": l.location,
            "status": l.status,
            "channel": l.channel,
            "appointment_at": l.appointment_at.isoformat() if l.appointment_at else None,
            "last_message_at": l.last_message_at.isoformat() if l.last_message_at else None,
            "created_at": l.created_at.isoformat(),
        }
        for l in leads
    ]


@router.get("/leads/{lead_id}")
def get_lead(
    lead_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get full lead data."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    business = db.query(Business).filter(
        Business.id == lead.business_id,
        Business.owner_id == current_user.id,
    ).first()
    if not business:
        raise HTTPException(status_code=403, detail="Access denied")
    return {
        "id": str(lead.id),
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "language": lead.language,
        "service_requested": lead.service_requested,
        "location": lead.location,
        "location_valid": lead.location_valid,
        "notes": lead.notes,
        "status": lead.status,
        "channel": lead.channel,
        "appointment_at": lead.appointment_at.isoformat() if lead.appointment_at else None,
        "follow_up_count": lead.follow_up_count,
        "follow_up_scheduled_at": lead.follow_up_scheduled_at.isoformat() if lead.follow_up_scheduled_at else None,
        "last_message_at": lead.last_message_at.isoformat() if lead.last_message_at else None,
        "created_at": lead.created_at.isoformat(),
    }


class UpdateLeadStatusRequest(BaseModel):
    status: str


@router.patch("/leads/{lead_id}/status")
def update_lead_status(
    lead_id: str,
    payload: UpdateLeadStatusRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update lead status."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    business = db.query(Business).filter(
        Business.id == lead.business_id,
        Business.owner_id == current_user.id,
    ).first()
    if not business:
        raise HTTPException(status_code=403, detail="Access denied")
    valid_statuses = {"new", "qualified", "scheduled", "follow_up", "waiting_human", "cold", "won", "lost"}
    if payload.status not in valid_statuses:
        raise HTTPException(status_code=422, detail="Invalid status")
    lead.status = payload.status
    db.commit()
    return {"status": lead.status}


@router.get("/leads/{lead_id}/messages")
def get_lead_messages(
    lead_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get full conversation history for a lead."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # Verify ownership through business
    business = db.query(Business).filter(
        Business.id == lead.business_id,
        Business.owner_id == current_user.id,
    ).first()
    if not business:
        raise HTTPException(status_code=403, detail="Access denied")

    conversations = (
        db.query(Conversation)
        .filter(Conversation.lead_id == lead.id)
        .order_by(Conversation.created_at.asc())
        .all()
    )

    result = []
    for conv in conversations:
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc())
            .all()
        )
        result.extend([
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ])

    return {"lead_id": lead_id, "messages": result}
