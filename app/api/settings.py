"""
Settings API — notifications, profile, usage stats, and lead sources.
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import extract
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from app.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.business import Business
from app.models.lead import Lead
from app.models.service_zone import ServiceZone
from app.models.channel import Channel

router = APIRouter(prefix="/api/settings", tags=["settings"])

PLAN_LIMITS = {"trial": 30, "starter": 100, "pro": 300, "scale": 1000}


class NotificationsIn(BaseModel):
    notify_on_new_lead: bool = True
    notify_on_escalate: bool = True
    notify_phone: Optional[str] = None
    quiet_hours_start: Optional[int] = None
    quiet_hours_end: Optional[int] = None


class ProfileIn(BaseModel):
    name: str
    owner_name: str
    phone: str
    city: str
    country: str = "US"
    website: Optional[str] = None


class ZoneIn(BaseModel):
    type: str  # neighborhoods / cities / zipcodes / radius
    locations: List[str] = []
    radius_km: Optional[int] = None


@router.get("")
def get_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        return {"business": None, "stats": None}

    now = datetime.utcnow()
    leads_month = db.query(Lead).filter(
        Lead.business_id == business.id,
        extract("month", Lead.created_at) == now.month,
        extract("year", Lead.created_at) == now.year,
    ).count()
    scheduled_month = db.query(Lead).filter(
        Lead.business_id == business.id,
        Lead.status == "scheduled",
        extract("month", Lead.created_at) == now.month,
        extract("year", Lead.created_at) == now.year,
    ).count()

    zone = db.query(ServiceZone).filter(ServiceZone.business_id == business.id).first()

    return {
        "business": {
            "id": str(business.id),
            "name": business.name,
            "owner_name": business.owner_name,
            "phone": business.phone,
            "city": business.city,
            "country": business.country,
            "website": business.website,
            "plan": business.plan or "trial",
            "notify_on_new_lead": business.notify_on_new_lead,
            "notify_on_escalate": business.notify_on_escalate,
            "notify_phone": business.notify_phone,
            "quiet_hours_start": business.quiet_hours_start,
            "quiet_hours_end": business.quiet_hours_end,
        },
        "zone": {
            "type": zone.type if zone else "neighborhoods",
            "locations": zone.locations or [] if zone else [],
            "radius_km": zone.radius_km if zone else None,
        } if zone else None,
        "stats": {
            "leads_this_month": leads_month,
            "scheduled_this_month": scheduled_month,
            "plan_limit": PLAN_LIMITS.get(business.plan or "trial", 30),
        },
    }


@router.put("/notifications")
def save_notifications(
    data: NotificationsIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        return {"error": "Business not found"}
    business.notify_on_new_lead = data.notify_on_new_lead
    business.notify_on_escalate = data.notify_on_escalate
    business.notify_phone = data.notify_phone or None
    business.quiet_hours_start = data.quiet_hours_start
    business.quiet_hours_end = data.quiet_hours_end
    db.commit()
    return {"ok": True}


@router.post("/send-report")
def send_report_now(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trigger the daily WhatsApp report immediately."""
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business or not business.notify_phone:
        return {"error": "No notify_phone configured"}
    from app.services.notify_service import send_daily_report
    send_daily_report(business, db)
    return {"ok": True}


@router.put("/zones")
def save_zones(
    data: ZoneIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        return {"error": "Business not found"}
    zone = db.query(ServiceZone).filter(ServiceZone.business_id == business.id).first()
    if zone:
        zone.type = data.type
        zone.locations = data.locations
        zone.radius_km = data.radius_km
    else:
        db.add(ServiceZone(
            id=uuid.uuid4(),
            business_id=business.id,
            type=data.type,
            locations=data.locations,
            radius_km=data.radius_km,
        ))
    db.commit()
    return {"ok": True}


@router.put("/profile")
def save_profile(
    data: ProfileIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        return {"error": "Business not found"}
    business.name = data.name
    business.owner_name = data.owner_name
    business.phone = data.phone
    business.city = data.city
    business.country = data.country
    business.website = data.website or None
    db.commit()
    return {"ok": True}


# ── Lead Sources ──────────────────────────────────────────────────────────────

class FacebookSourceIn(BaseModel):
    page_id: str
    page_name: Optional[str] = None
    access_token: str


class GoogleSourceIn(BaseModel):
    label: Optional[str] = None  # e.g. "Plumber campaign"


@router.get("/lead-sources")
def get_lead_sources(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        return {"sources": []}

    channels = db.query(Channel).filter(
        Channel.business_id == business.id,
        Channel.provider.in_(["meta", "google_ads"]),
    ).all()

    sources = []
    for ch in channels:
        cfg = ch.config or {}
        sources.append({
            "id": str(ch.id),
            "provider": ch.provider,
            "label": cfg.get("page_name") or cfg.get("label") or ch.identifier,
            "identifier": ch.identifier,
            "active": ch.active,
            "webhook_url": _google_webhook_url(ch.identifier) if ch.provider == "google_ads" else None,
        })
    return {"sources": sources}


@router.post("/lead-sources/facebook")
def connect_facebook(
    data: FacebookSourceIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        raise HTTPException(404, "Business not found")

    existing = db.query(Channel).filter(
        Channel.business_id == business.id,
        Channel.identifier == data.page_id,
        Channel.provider == "meta",
    ).first()

    if existing:
        existing.config = {"access_token": data.access_token, "page_name": data.page_name}
        existing.active = True
    else:
        db.add(Channel(
            id=uuid.uuid4(),
            business_id=business.id,
            type="facebook_leads",
            provider="meta",
            identifier=data.page_id,
            active=True,
            config={"access_token": data.access_token, "page_name": data.page_name},
        ))
    db.commit()
    return {"ok": True, "page_id": data.page_id}


@router.post("/lead-sources/google")
def connect_google(
    data: GoogleSourceIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        raise HTTPException(404, "Business not found")

    token = uuid.uuid4().hex  # unique per connection
    db.add(Channel(
        id=uuid.uuid4(),
        business_id=business.id,
        type="google_leads",
        provider="google_ads",
        identifier=token,
        active=True,
        config={"label": data.label},
    ))
    db.commit()
    return {"ok": True, "token": token, "webhook_url": _google_webhook_url(token)}


@router.delete("/lead-sources/{channel_id}")
def disconnect_source(
    channel_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        raise HTTPException(404, "Business not found")

    ch = db.query(Channel).filter(
        Channel.id == channel_id,
        Channel.business_id == business.id,
    ).first()
    if not ch:
        raise HTTPException(404, "Source not found")

    db.delete(ch)
    db.commit()
    return {"ok": True}


def _google_webhook_url(token: str) -> str:
    from app.config import settings
    backend = "https://backend-production-558d.up.railway.app"
    return f"{backend}/api/webhooks/google-leads?token={token}"
