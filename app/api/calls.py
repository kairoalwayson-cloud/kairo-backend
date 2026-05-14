from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.business import Business
from app.models.call_log import CallLog
from app.models.lead import Lead

router = APIRouter(prefix="/api/calls", tags=["calls"])


def _get_business(db: Session, user: User) -> Business:
    biz = db.query(Business).filter(Business.owner_id == user.id).first()
    if not biz:
        raise HTTPException(404, "Business not found")
    return biz


def _enrich(call: CallLog, db: Session, business_id) -> dict:
    lead_name = None
    lead_id = None
    lead_status = None
    lead_service = None
    appointment_at = None

    lead = None
    if call.lead_id:
        lead = db.query(Lead).filter(Lead.id == call.lead_id).first()
    elif call.caller_phone:
        lead = db.query(Lead).filter(
            Lead.business_id == business_id,
            Lead.phone == call.caller_phone,
        ).order_by(Lead.created_at.desc()).first()

    if lead:
        lead_name = lead.name
        lead_id = str(lead.id)
        lead_status = lead.status
        lead_service = lead.service_requested
        if lead.appointment_at:
            appointment_at = lead.appointment_at.isoformat()

    converted = lead_status == "scheduled" and appointment_at is not None

    return {
        "id": str(call.id),
        "caller_phone": call.caller_phone,
        "lead_name": lead_name,
        "lead_id": lead_id,
        "lead_service": lead_service,
        "duration_seconds": call.duration_seconds,
        "status": call.status,
        "converted": converted,
        "appointment_at": appointment_at,
        "summary": call.summary,
        "has_transcript": bool(call.transcript),
        "recording_url": call.recording_url,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "created_at": call.created_at.isoformat() if call.created_at else None,
    }


@router.get("")
def list_calls(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    biz = _get_business(db, current_user)

    if biz.plan in ("trial", "starter"):
        return {"calls": [], "total": 0, "plan_gate": True, "current_plan": biz.plan}

    q = (
        db.query(CallLog)
        .filter(CallLog.business_id == biz.id)
        .order_by(CallLog.created_at.desc())
    )
    total = q.count()
    calls = q.offset((page - 1) * per_page).limit(per_page).all()
    return {
        "calls": [_enrich(c, db, biz.id) for c in calls],
        "total": total,
        "page": page,
        "per_page": per_page,
        "plan_gate": False,
    }


@router.get("/{call_id}")
def get_call(
    call_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    biz = _get_business(db, current_user)

    if biz.plan in ("trial", "starter"):
        raise HTTPException(403, "Upgrade to Pro to access call logs")

    call = db.query(CallLog).filter(
        CallLog.id == call_id,
        CallLog.business_id == biz.id,
    ).first()
    if not call:
        raise HTTPException(404, "Call not found")

    data = _enrich(call, db, biz.id)
    data["transcript"] = call.transcript
    data["messages"] = call.messages_json
    return data
