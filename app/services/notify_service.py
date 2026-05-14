"""
Send WhatsApp notifications and reports to the business owner.
"""
import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)


def _is_quiet_hours(business) -> bool:
    start = business.quiet_hours_start
    end = business.quiet_hours_end
    if start is None or end is None:
        return False
    hour = datetime.utcnow().hour
    if start > end:  # crosses midnight e.g. 22 → 8
        return hour >= start or hour < end
    return start <= hour < end


def send_owner_notification(business, message: str) -> None:
    """Send a WhatsApp message to the owner's notify_phone if enabled and not in quiet hours."""
    if not business.notify_phone:
        return
    if _is_quiet_hours(business):
        logger.info(f"Quiet hours active — skipping notification for {business.id}")
        return
    _send_whatsapp(business.notify_phone, message)


def _send_whatsapp(to_phone: str, message: str) -> None:
    try:
        from twilio.rest import Client
        from app.config import settings
        if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN):
            return
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=f"whatsapp:{settings.TWILIO_WHATSAPP_NUMBER}",
            body=message,
            to=f"whatsapp:{to_phone}",
        )
        logger.info(f"WhatsApp sent to {to_phone}")
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")


def send_appointment_report(business, lead, appointment_dt: Optional[datetime], messages: list) -> None:
    """Send a detailed conversation report after an appointment is booked."""
    if not business.notify_phone:
        return
    if _is_quiet_hours(business):
        return

    dt_str = appointment_dt.strftime("%d/%m/%Y às %H:%M") if appointment_dt else "data a confirmar"
    service = lead.service_requested or "Serviço não especificado"
    location = lead.location or "—"
    name = lead.name or "Cliente"
    phone = lead.phone or "—"

    # Last 6 messages formatted
    convo_lines = []
    for m in messages[-6:]:
        role_label = "Cliente" if m.get("role") == "user" else (lead.business.ai_name if hasattr(lead, "business") else "IA")
        text = m.get("content", "")[:120]
        convo_lines.append(f"> *{role_label}:* {text}")
    convo_block = "\n".join(convo_lines) if convo_lines else "_Conversa via voz_"

    report = (
        f"✅ *Agendamento Confirmado*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 *Cliente:* {name}\n"
        f"📱 *Telefone:* {phone}\n"
        f"🔧 *Serviço:* {service}\n"
        f"📍 *Local:* {location}\n"
        f"📅 *Data:* {dt_str}\n\n"
        f"💬 *Últimas mensagens:*\n"
        f"{convo_block}\n\n"
        f"_Kairo AI · {business.name}_"
    )
    _send_whatsapp(business.notify_phone, report)


def send_daily_report(business, db) -> None:
    """Send a daily summary of leads and appointments."""
    if not business.notify_phone:
        return

    from app.models.lead import Lead
    from sqlalchemy import extract
    now = datetime.utcnow()

    today_leads = db.query(Lead).filter(
        Lead.business_id == business.id,
        extract("day", Lead.created_at) == now.day,
        extract("month", Lead.created_at) == now.month,
        extract("year", Lead.created_at) == now.year,
    ).all()

    scheduled = [l for l in today_leads if l.status == "scheduled"]
    pending = [l for l in today_leads if l.status in ("new", "qualified", "follow_up")]
    attention = [l for l in today_leads if l.status == "waiting_human"]

    if not today_leads:
        return  # Nothing to report

    lines = []
    for l in today_leads[:8]:
        status_icon = "✅" if l.status == "scheduled" else ("⚡" if l.status == "waiting_human" else "⏳")
        dt = l.appointment_at.strftime("%d/%m %H:%M") if l.appointment_at else ""
        svc = l.service_requested or ""
        name = l.name or l.phone or "Lead"
        lines.append(f"{status_icon} {name} — {svc}{' · ' + dt if dt else ''}")

    body = (
        f"📊 *Relatório do Dia — {now.strftime('%d/%m')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📥 *Leads hoje:* {len(today_leads)}\n"
        f"✅ *Agendamentos:* {len(scheduled)}\n"
        f"⏳ *Pendentes:* {len(pending)}\n"
        f"⚡ *Precisam atenção:* {len(attention)}\n\n"
        f"*Detalhes:*\n" + "\n".join(lines) + "\n\n"
        f"_Kairo AI · {business.name}_"
    )
    _send_whatsapp(business.notify_phone, body)
