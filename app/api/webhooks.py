"""
Webhook endpoints — receive messages from Twilio (WhatsApp + SMS) and other channels.
These are public endpoints (no auth) — Twilio calls them directly.
"""
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import Optional
import logging
from app.database import get_db
from app.models.business import Business
from app.models.channel import Channel
from app.services.conversation_service import process_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _find_business_by_twilio_number(db: Session, to_number: str) -> Optional[Business]:
    """Find which business owns the Twilio number that received the message."""
    channel = db.query(Channel).filter(
        Channel.identifier == to_number,
        Channel.provider == "twilio",
        Channel.active == True,
    ).first()
    if channel:
        return db.query(Business).filter(Business.id == channel.business_id).first()
    return None


@router.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db: Session = Depends(get_db),
    From: str = Form(...),       # sender's WhatsApp number e.g. "whatsapp:+15551234567"
    To: str = Form(...),         # Kairo's number e.g. "whatsapp:+15559876543"
    Body: str = Form(""),        # message text
    NumMedia: str = Form("0"),   # number of media files
    MediaUrl0: Optional[str] = Form(None),  # photo URL if sent
):
    """
    Twilio WhatsApp webhook.
    Twilio sends a POST here when a message arrives.
    We must respond with TwiML or empty 200 (Twilio sends the reply via REST API).
    """
    sender_phone = From.replace("whatsapp:", "").strip()
    to_number = To.replace("whatsapp:", "").strip()

    logger.info(f"WhatsApp message from {sender_phone} to {to_number}: {Body[:50]}")

    business = _find_business_by_twilio_number(db, to_number)
    if not business:
        logger.warning(f"No business found for number {to_number}")
        return Response(content="", media_type="text/xml")

    # Process through AI engine
    result = process_message(
        db=db,
        business_id=business.id,
        channel="whatsapp",
        sender_id=sender_phone,
        text=Body or "(no text — possible media message)",
    )

    reply_text = result["response_text"]
    logger.info(f"AI response for {sender_phone}: {reply_text[:80]}")

    # Notify owner about new lead or escalation
    try:
        from app.services.notify_service import send_owner_notification
        lead_id = result.get("lead_id")
        if lead_id and business.notify_on_new_lead:
            from app.models.lead import Lead
            lead = db.query(Lead).filter(Lead.id == lead_id).first()
            is_new = lead and lead.last_message_at == lead.created_at
            if is_new:
                name = lead.name or sender_phone
                svc = lead.service_requested or ""
                send_owner_notification(business, f"🔔 Novo lead: {name}{' — ' + svc if svc else ''} (WhatsApp)")
        if result.get("escalate") and business.notify_on_escalate:
            lead = None
            if result.get("lead_id"):
                from app.models.lead import Lead
                lead = db.query(Lead).filter(Lead.id == result["lead_id"]).first()
            name = (lead.name if lead else None) or sender_phone
            send_owner_notification(business, f"⚡ {name} quer falar com você agora!")
    except Exception as e:
        logger.error(f"Owner notification error: {e}")

    # Send reply via Twilio REST API
    try:
        from twilio.rest import Client
        from app.config import settings
        if settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN:
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            client.messages.create(
                from_=f"whatsapp:{to_number}",
                body=reply_text,
                to=f"whatsapp:{sender_phone}",
            )
            logger.info(f"Reply sent to {sender_phone}")
        else:
            logger.warning("Twilio credentials not configured — reply not sent")
    except Exception as e:
        logger.error(f"Failed to send Twilio reply: {e}")

    return Response(content="", media_type="text/xml", status_code=200)


@router.post("/sms")
async def sms_webhook(
    db: Session = Depends(get_db),
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(""),
):
    """Twilio SMS webhook."""
    business = _find_business_by_twilio_number(db, To)
    if not business:
        return Response(content="", media_type="text/xml")

    result = process_message(
        db=db,
        business_id=business.id,
        channel="sms",
        sender_id=From,
        text=Body,
    )

    reply_text = result["response_text"]
    logger.info(f"SMS from {From}: reply = {reply_text[:80]}")

    # Notify owner about new lead or escalation
    try:
        from app.services.notify_service import send_owner_notification
        lead_id = result.get("lead_id")
        if lead_id and business.notify_on_new_lead:
            from app.models.lead import Lead
            lead = db.query(Lead).filter(Lead.id == lead_id).first()
            is_new = lead and lead.last_message_at == lead.created_at
            if is_new:
                name = lead.name or From
                svc = lead.service_requested or ""
                send_owner_notification(business, f"🔔 Novo lead: {name}{' — ' + svc if svc else ''} (SMS)")
        if result.get("escalate") and business.notify_on_escalate:
            lead = None
            if result.get("lead_id"):
                from app.models.lead import Lead
                lead = db.query(Lead).filter(Lead.id == result["lead_id"]).first()
            name = (lead.name if lead else None) or From
            send_owner_notification(business, f"⚡ {name} quer falar com você agora!")
    except Exception as e:
        logger.error(f"Owner notification error: {e}")

    # Send reply via Twilio REST API
    try:
        from twilio.rest import Client
        from app.config import settings
        if settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN:
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            client.messages.create(
                from_=To,
                body=reply_text,
                to=From,
            )
            logger.info(f"SMS reply sent to {From}")
        else:
            logger.warning("Twilio credentials not configured — SMS reply not sent")
    except Exception as e:
        logger.error(f"Failed to send SMS reply: {e}")

    return Response(content="", media_type="text/xml", status_code=200)


@router.get("/whatsapp")
async def whatsapp_verify():
    """Health check for webhook URL validation."""
    return {"status": "ok", "webhook": "whatsapp"}


# ── Facebook Lead Ads ─────────────────────────────────────────────────────────

@router.get("/facebook")
async def facebook_verify(
    hub_mode: Optional[str] = None,
    hub_verify_token: Optional[str] = None,
    hub_challenge: Optional[str] = None,
):
    """Facebook webhook verification challenge."""
    from app.config import settings
    from fastapi.responses import PlainTextResponse
    if hub_mode == "subscribe" and hub_verify_token == settings.FACEBOOK_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(403, "Invalid verify token")


@router.post("/facebook")
async def facebook_leads(request: Request, db: Session = Depends(get_db)):
    """Receive Facebook Lead Ads events and create leads."""
    import httpx
    body = await request.json()
    logger.info(f"Facebook webhook: {str(body)[:200]}")

    for entry in body.get("entry", []):
        page_id = str(entry.get("id", ""))
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            value = change.get("value", {})
            leadgen_id = value.get("leadgen_id")
            if not leadgen_id:
                continue

            # Find business by page_id
            channel = db.query(Channel).filter(
                Channel.identifier == page_id,
                Channel.provider == "meta",
                Channel.active == True,
            ).first()
            if not channel:
                logger.warning(f"No business found for Facebook page {page_id}")
                continue

            business = db.query(Business).filter(Business.id == channel.business_id).first()
            if not business:
                continue

            access_token = (channel.config or {}).get("access_token")
            if not access_token:
                logger.warning(f"No access token for page {page_id}")
                continue

            # Fetch lead data from Graph API
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"https://graph.facebook.com/v19.0/{leadgen_id}",
                        params={"access_token": access_token, "fields": "field_data,created_time"},
                        timeout=10,
                    )
                    lead_data = resp.json()
            except Exception as e:
                logger.error(f"Facebook Graph API error: {e}")
                continue

            # Parse field_data into dict
            fields: dict = {}
            for f in lead_data.get("field_data", []):
                fields[f["name"]] = f["values"][0] if f.get("values") else ""

            name = fields.get("full_name") or fields.get("first_name", "") + " " + fields.get("last_name", "")
            name = name.strip() or None
            phone = fields.get("phone_number") or fields.get("phone") or None
            email = fields.get("email") or None
            service = fields.get("service") or fields.get("what_service") or None

            _create_and_followup_lead(
                db=db,
                business=business,
                name=name,
                phone=phone,
                email=email,
                service=service,
                source="facebook",
                meta={"leadgen_id": leadgen_id, "page_id": page_id, "raw": fields},
            )

    return {"received": True}


# ── Google Ads Lead Form ──────────────────────────────────────────────────────

@router.post("/google-leads")
async def google_leads(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Receive Google Ads Lead Form submissions."""
    body = await request.json()
    logger.info(f"Google lead webhook token={token}: {str(body)[:200]}")

    # Find business by token stored in channels
    channel = db.query(Channel).filter(
        Channel.identifier == token,
        Channel.provider == "google_ads",
        Channel.active == True,
    ).first()
    if not channel:
        raise HTTPException(404, "Unknown token")

    business = db.query(Business).filter(Business.id == channel.business_id).first()
    if not business:
        raise HTTPException(404, "Business not found")

    # Google Ads lead form payload structure
    user_column_data = body.get("user_column_data", [])
    fields: dict = {item["column_name"]: item.get("string_value", "") for item in user_column_data}

    name = fields.get("FULL_NAME") or (fields.get("FIRST_NAME", "") + " " + fields.get("LAST_NAME", "")).strip() or None
    phone = fields.get("PHONE_NUMBER") or None
    email = fields.get("EMAIL") or None
    service = fields.get("SERVICE") or None

    _create_and_followup_lead(
        db=db,
        business=business,
        name=name,
        phone=phone,
        email=email,
        service=service,
        source="google",
        meta={"raw": body},
    )

    return {"received": True}


# ── Helper: create lead and send WhatsApp follow-up ──────────────────────────

def _create_and_followup_lead(
    db: Session,
    business: Business,
    name: Optional[str],
    phone: Optional[str],
    email: Optional[str],
    service: Optional[str],
    source: str,
    meta: dict,
):
    from app.models.lead import Lead
    from app.models.conversation import Conversation

    # Deduplicate: if same phone + business in last 24h, skip
    if phone:
        from datetime import datetime, timedelta
        recent = db.query(Lead).filter(
            Lead.business_id == business.id,
            Lead.phone == phone,
            Lead.created_at >= datetime.utcnow() - timedelta(hours=24),
        ).first()
        if recent:
            logger.info(f"Duplicate lead from {phone} — skipped")
            return

    lead = Lead(
        business_id=business.id,
        name=name,
        phone=phone,
        email=email,
        service_requested=service,
        channel=source,
        status="new",
        metadata_={**meta, "source": source},
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)

    logger.info(f"Lead created from {source}: {name or phone} → business {business.id}")

    # Notify owner
    try:
        from app.services.notify_service import send_owner_notification
        label = "Facebook Ads" if source == "facebook" else "Google Ads"
        n = name or phone or "Sem nome"
        svc = f" — {service}" if service else ""
        send_owner_notification(business, f"🎯 Novo lead via {label}: {n}{svc}")
    except Exception as e:
        logger.error(f"Owner notification error: {e}")

    # Send initial WhatsApp message if lead has phone
    if phone and business.notify_phone:
        try:
            from app.services.conversation_service import process_message
            process_message(
                db=db,
                business_id=business.id,
                channel="whatsapp",
                sender_id=phone,
                text=f"Olá, vim pelo anúncio" + (f" de {service}" if service else ""),
            )
        except Exception as e:
            logger.error(f"Initial message error: {e}")
