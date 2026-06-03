from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
import uuid

from app.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.business import Business
from app.models.service import Service
from app.models.service_zone import ServiceZone
from app.services.vapi_service import create_or_update_assistant, PHONE_NUMBER_POOL
from app.models.channel import Channel

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def _build_zone_text(zones: list) -> str:
    if not zones:
        return "All locations accepted."
    z = zones[0]
    if z.type == "radius":
        return f"Serves within {z.radius_km}km radius of base location. REJECT callers clearly outside this area."
    if z.type == "zipcodes" and z.locations:
        return f"Serves ONLY these zip codes: {', '.join(z.locations)}. REJECT any caller from a different zip code."
    if z.locations:
        label = "cities" if z.type == "cities" else "neighborhoods"
        locs = ", ".join(z.locations)
        return (
            f"Serves ONLY these {label}: {locs}. "
            f"REJECT any caller from a different city, state, or region — even if it sounds similar. "
            f"Only accept if the caller's city matches one of these exactly."
        )
    return "All locations accepted."


def _get_or_create_business(db: Session, user: User) -> Business:
    business = db.query(Business).filter(Business.owner_id == user.id).first()
    if not business:
        business = Business(
            id=uuid.uuid4(),
            owner_id=user.id,
            name=f"{user.full_name}'s Business",
            owner_name=user.full_name,
        )
        db.add(business)
        db.commit()
        db.refresh(business)
    return business


class BusinessProfileIn(BaseModel):
    name: str
    owner_name: str
    phone: str
    city: str
    country: str = "US"
    website: Optional[str] = None


class CategoriesIn(BaseModel):
    categories: List[str]


class ServiceIn(BaseModel):
    name: str
    description: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    price_unit: str = "visit"
    accepts_photo_quote: bool = False


class ServicesIn(BaseModel):
    services: List[ServiceIn]


class ZoneIn(BaseModel):
    type: str  # radius / neighborhoods / zipcodes
    radius_km: Optional[int] = None
    center_address: Optional[str] = None  # display only, not persisted
    locations: Optional[List[str]] = []


class HoursIn(BaseModel):
    working_hours: dict
    timezone: str = "America/New_York"
    emergency_attendance: bool = False


class AIConfigIn(BaseModel):
    ai_name: str = "Maya"
    ai_gender: str = "feminino"
    ai_tone: str = "semi-formal"
    ai_language: str = "en"
    ai_detect_language: bool = True
    welcome_message: Optional[str] = None


class PlanIn(BaseModel):
    plan: str  # starter / pro / scale


@router.get("/status")
def onboarding_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    services = db.query(Service).filter(Service.business_id == business.id).all() if business else []
    return {
        "onboarding_completed": current_user.onboarding_completed,
        "onboarding_step": current_user.onboarding_step,
        "business": {
            "id": str(business.id),
            "name": business.name,
            "owner_name": business.owner_name,
            "phone": business.phone,
            "city": business.city,
            "country": business.country,
            "website": business.website,
            "categories": business.categories or [],
            "ai_name": business.ai_name,
            "ai_gender": business.ai_gender,
            "ai_tone": business.ai_tone,
            "ai_language": business.ai_language,
            "ai_detect_language": business.ai_detect_language,
            "welcome_message": business.welcome_message,
            "working_hours": business.working_hours,
            "timezone": business.timezone,
            "emergency_attendance": business.emergency_attendance,
            "plan": business.plan,
            "services": [
                {
                    "name": s.name,
                    "description": s.description,
                    "price_min": float(s.price_min) if s.price_min else None,
                    "price_max": float(s.price_max) if s.price_max else None,
                    "price_unit": s.price_unit,
                    "accepts_photo_quote": s.accepts_photo_quote,
                }
                for s in services
            ],
        } if business else None,
    }


@router.post("/business")
def save_business(
    data: BusinessProfileIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = _get_or_create_business(db, current_user)
    business.name = data.name
    business.owner_name = data.owner_name
    business.phone = data.phone
    business.city = data.city
    business.country = data.country
    business.website = data.website
    current_user.onboarding_step = "categories"
    db.commit()
    return {"business_id": str(business.id), "next_step": "categories"}


@router.put("/categories")
def save_categories(
    data: CategoriesIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = _get_or_create_business(db, current_user)
    business.categories = data.categories
    current_user.onboarding_step = "services"
    db.commit()
    return {"next_step": "services"}


@router.put("/services")
def save_services(
    data: ServicesIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = _get_or_create_business(db, current_user)
    db.query(Service).filter(Service.business_id == business.id).delete()
    for svc in data.services:
        db.add(Service(
            id=uuid.uuid4(),
            business_id=business.id,
            name=svc.name,
            description=svc.description,
            price_min=svc.price_min,
            price_max=svc.price_max,
            price_unit=svc.price_unit,
            accepts_photo_quote=svc.accepts_photo_quote,
        ))
    current_user.onboarding_step = "zones"
    db.commit()
    return {"next_step": "zones", "count": len(data.services)}


@router.put("/zones")
def save_zones(
    data: ZoneIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = _get_or_create_business(db, current_user)
    db.query(ServiceZone).filter(ServiceZone.business_id == business.id).delete()
    db.add(ServiceZone(
        id=uuid.uuid4(),
        business_id=business.id,
        type=data.type,
        radius_km=data.radius_km,
        locations=data.locations or [],
    ))
    current_user.onboarding_step = "hours"
    db.commit()
    return {"next_step": "hours"}


@router.put("/hours")
def save_hours(
    data: HoursIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = _get_or_create_business(db, current_user)
    business.working_hours = data.working_hours
    business.timezone = data.timezone
    business.emergency_attendance = data.emergency_attendance
    current_user.onboarding_step = "ai_config"
    db.commit()
    return {"next_step": "ai_config"}


@router.put("/ai-config")
def save_ai_config(
    data: AIConfigIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = _get_or_create_business(db, current_user)
    business.ai_name = data.ai_name
    business.ai_gender = data.ai_gender
    business.ai_tone = data.ai_tone
    business.ai_language = data.ai_language
    business.ai_detect_language = data.ai_detect_language
    business.welcome_message = data.welcome_message
    current_user.onboarding_step = "channels"
    db.commit()

    # Sync Vapi assistant (non-blocking — failure doesn't break onboarding)
    services = db.query(Service).filter(Service.business_id == business.id, Service.active == True).all()
    zones = db.query(ServiceZone).filter(ServiceZone.business_id == business.id).all()
    services_text = "\n".join(
        f"- {s.name}" + (f" (${s.price_min:.0f}–${s.price_max:.0f})" if s.price_min and s.price_max else "")
        for s in services
    ) or "Services not configured yet."
    zone_text = _build_zone_text(zones)
    assistant_id = create_or_update_assistant(
        business_name=business.name,
        ai_name=data.ai_name,
        ai_gender=data.ai_gender,
        ai_tone=data.ai_tone,
        services_text=services_text,
        zone_text=zone_text,
        existing_assistant_id=business.vapi_assistant_id,
    )
    if assistant_id and assistant_id != business.vapi_assistant_id:
        business.vapi_assistant_id = assistant_id
        db.commit()

    # Ensure SMS channel exists for each Twilio number in the pool
    for twilio_number in PHONE_NUMBER_POOL:
        existing = db.query(Channel).filter(
            Channel.business_id == business.id,
            Channel.provider == "twilio",
            Channel.type == "sms",
        ).first()
        if not existing:
            db.add(Channel(
                business_id=business.id,
                type="sms",
                provider="twilio",
                identifier=twilio_number,
                active=True,
            ))
            db.commit()

    return {"next_step": "channels", "vapi_assistant_id": assistant_id}


@router.put("/plan")
def save_plan(
    data: PlanIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = _get_or_create_business(db, current_user)
    business.plan = data.plan
    current_user.onboarding_step = "complete"
    db.commit()
    return {"next_step": "complete", "plan": data.plan}


@router.post("/complete")
def complete_onboarding(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.onboarding_completed = True
    current_user.onboarding_step = "complete"
    db.commit()
    return {"onboarding_completed": True}
