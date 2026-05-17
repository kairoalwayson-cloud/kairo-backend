"""
Twilio Phone Number — search, purchase, verify, release.
Each business gets one dedicated Twilio number linked to their Vapi assistant.
"""
import httpx
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.business import Business

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/phone", tags=["phone"])

VAPI_BASE = "https://api.vapi.ai"


def _vapi_headers():
    return {
        "Authorization": f"Bearer {settings.VAPI_API_KEY}",
        "Content-Type": "application/json",
    }


def _twilio_client():
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise HTTPException(500, "Twilio not configured")
    from twilio.rest import Client
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


@router.get("/status")
def phone_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        raise HTTPException(404, "Business not found")
    return {
        "phone_number": business.twilio_phone_number,
        "phone_sid": business.twilio_phone_sid,
        "vapi_phone_id": business.vapi_phone_id,
        "phone_verified": business.phone_verified,
    }


@router.get("/search")
def search_numbers(
    area_code: str = Query(..., min_length=3, max_length=3),
    current_user: User = Depends(get_current_user),
):
    client = _twilio_client()
    try:
        numbers = client.available_phone_numbers("US").local.list(area_code=area_code, limit=6)
        return {
            "numbers": [
                {
                    "phone_number": n.phone_number,
                    "friendly_name": n.friendly_name,
                    "locality": n.locality or "",
                    "region": n.region or "",
                }
                for n in numbers
            ]
        }
    except Exception as e:
        logger.error("Twilio search error: %s", e)
        raise HTTPException(500, f"Search failed: {str(e)}")


class PurchaseRequest(BaseModel):
    phone_number: str


@router.post("/purchase")
def purchase_number(
    payload: PurchaseRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        raise HTTPException(404, "Business not found")
    if business.twilio_phone_number:
        raise HTTPException(409, "Business already has a dedicated phone number")

    client = _twilio_client()

    # 1. Purchase via Twilio
    try:
        purchased = client.incoming_phone_numbers.create(phone_number=payload.phone_number)
        phone_sid = purchased.sid
        logger.info("Twilio number purchased: %s (%s)", payload.phone_number, phone_sid)
    except Exception as e:
        logger.error("Twilio purchase error: %s", e)
        raise HTTPException(500, f"Purchase failed: {str(e)}")

    # 2. Register on Vapi and link to the business's assistant
    vapi_phone_id = None
    if settings.VAPI_API_KEY:
        try:
            vapi_payload: dict = {
                "provider": "twilio",
                "number": payload.phone_number,
                "twilioAccountSid": settings.TWILIO_ACCOUNT_SID,
                "twilioAuthToken": settings.TWILIO_AUTH_TOKEN,
                "name": business.name,
            }
            if business.vapi_assistant_id:
                vapi_payload["assistantId"] = business.vapi_assistant_id
            resp = httpx.post(
                f"{VAPI_BASE}/phone-number",
                headers=_vapi_headers(),
                json=vapi_payload,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                vapi_phone_id = resp.json().get("id")
                logger.info("Vapi phone number created: %s", vapi_phone_id)
            else:
                logger.error("Vapi phone create failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Vapi phone registration error: %s", e)

    # 3. Save to business
    business.twilio_phone_sid = phone_sid
    business.twilio_phone_number = payload.phone_number
    business.vapi_phone_id = vapi_phone_id
    business.phone_verified = False
    db.commit()

    return {
        "ok": True,
        "phone_number": payload.phone_number,
        "phone_sid": phone_sid,
        "vapi_phone_id": vapi_phone_id,
    }


@router.post("/verify/send")
def send_verification(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business or not business.twilio_phone_number:
        raise HTTPException(400, "No phone number to verify")

    to_phone = business.notify_phone or business.phone
    if not to_phone:
        raise HTTPException(400, "No owner phone configured — set it in Notifications first")

    client = _twilio_client()
    try:
        msg = client.messages.create(
            body=f"Your Kairo number {business.twilio_phone_number} is active! This is a test message from your AI receptionist.",
            from_=business.twilio_phone_number,
            to=to_phone,
        )
        logger.info("Verification SMS sent %s → %s", business.twilio_phone_number, to_phone)
        return {"ok": True, "message_sid": msg.sid, "sent_to": to_phone}
    except Exception as e:
        logger.error("Verification SMS error: %s", e)
        raise HTTPException(500, f"SMS failed: {str(e)}")


@router.post("/verify/confirm")
def confirm_verification(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        raise HTTPException(404, "Business not found")
    business.phone_verified = True
    db.commit()
    return {"ok": True}


@router.post("/connect-ai")
def connect_to_ai(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retry Vapi phone registration for an already-purchased Twilio number."""
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business or not business.twilio_phone_number:
        raise HTTPException(400, "No purchased phone number found")
    if not settings.VAPI_API_KEY:
        raise HTTPException(500, "VAPI_API_KEY not configured")

    vapi_payload: dict = {
        "provider": "twilio",
        "number": business.twilio_phone_number,
        "twilioAccountSid": settings.TWILIO_ACCOUNT_SID,
        "twilioAuthToken": settings.TWILIO_AUTH_TOKEN,
        "name": business.name,
    }
    if business.vapi_assistant_id:
        vapi_payload["assistantId"] = business.vapi_assistant_id

    try:
        resp = httpx.post(
            f"{VAPI_BASE}/phone-number",
            headers=_vapi_headers(),
            json=vapi_payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            vapi_phone_id = resp.json().get("id")
            business.vapi_phone_id = vapi_phone_id
            db.commit()
            return {"ok": True, "vapi_phone_id": vapi_phone_id}
        else:
            logger.error("Vapi connect-ai failed: %s %s", resp.status_code, resp.text[:400])
            raise HTTPException(502, f"Vapi error {resp.status_code}: {resp.text[:300]}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Vapi connect-ai exception: %s", e)
        raise HTTPException(500, str(e))


@router.delete("/release")
def release_number(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business or not business.twilio_phone_sid:
        raise HTTPException(404, "No phone number to release")

    client = _twilio_client()

    # Release on Twilio
    try:
        client.incoming_phone_numbers(business.twilio_phone_sid).delete()
        logger.info("Twilio number released: %s", business.twilio_phone_number)
    except Exception as e:
        logger.error("Twilio release error: %s", e)

    # Remove from Vapi
    if business.vapi_phone_id and settings.VAPI_API_KEY:
        try:
            httpx.delete(
                f"{VAPI_BASE}/phone-number/{business.vapi_phone_id}",
                headers=_vapi_headers(),
                timeout=10,
            )
        except Exception as e:
            logger.error("Vapi phone delete error: %s", e)

    business.twilio_phone_sid = None
    business.twilio_phone_number = None
    business.vapi_phone_id = None
    business.phone_verified = False
    db.commit()

    return {"ok": True}
