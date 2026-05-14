"""
Dynamic System Prompt Generator
Builds the AI system prompt from the business configuration.
Called once per conversation session.
"""
from typing import Optional
from app.models.business import Business
from app.models.service import Service
from app.models.service_zone import ServiceZone


DAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _format_hours(working_hours: dict, language: str = "pt") -> str:
    if not working_hours:
        return "Monday to Friday, 8am to 6pm" if language == "en" else "Segunda a Sexta, 8h às 18h"

    days_map = {"pt": DAYS_PT, "en": DAYS_EN, "es": DAYS_ES}
    days = days_map.get(language, DAYS_PT)
    lines = []

    for i, key in enumerate(DAY_KEYS):
        day_data = working_hours.get(key, {})
        if not day_data.get("active", False):
            continue
        start = day_data.get("start", "08:00")
        end = day_data.get("end", "18:00")
        lunch = day_data.get("lunch", None)
        line = f"{days[i]}: {start} – {end}"
        if lunch:
            line += f" (lunch break {lunch['start']}–{lunch['end']})"
        lines.append(line)

    return "\n".join(lines) if lines else "Schedule not configured"


def _format_services(services: list[Service], language: str = "pt") -> str:
    if not services:
        return "Services not configured yet."

    currency = "$"
    lines = []
    for s in services:
        if not s.active:
            continue
        price_info = ""
        if s.price_min and s.price_max:
            price_info = f" ({currency}{s.price_min:.0f}–{currency}{s.price_max:.0f} per {s.price_unit})"
        elif s.price_min:
            price_info = f" (from {currency}{s.price_min:.0f})"
        desc = f" — {s.description}" if s.description else ""
        lines.append(f"• {s.name}{price_info}{desc}")

    return "\n".join(lines) if lines else "No active services."


def _format_zone(service_zones: list[ServiceZone], language: str = "pt") -> str:
    if not service_zones:
        return "Service area not defined — accept all locations for now."

    descriptions = []
    for zone in service_zones:
        if zone.type == "radius":
            descriptions.append(f"Within {zone.radius_km}km of the business location")
        elif zone.type == "neighborhoods" and zone.locations:
            descriptions.append("Neighborhoods/cities: " + ", ".join(zone.locations))
        elif zone.type == "zipcodes" and zone.locations:
            descriptions.append("ZIP codes: " + ", ".join(zone.locations))

    return "\n".join(descriptions) if descriptions else "Accept all locations."


def _escalation_rules_text(escalation_rules: list, language: str = "pt") -> str:
    rules = []
    for rule in escalation_rules:
        if not rule.active:
            continue
        if rule.trigger == "explicit_request":
            rules.append("- If the lead explicitly asks to speak with a human → escalate immediately")
        elif rule.trigger == "max_messages" and rule.max_messages:
            rules.append(f"- If the conversation exceeds {rule.max_messages} messages without resolution → escalate")
        elif rule.trigger == "high_value" and rule.threshold:
            rules.append(f"- If the estimated job value exceeds ${rule.threshold:.0f} → escalate")
        elif rule.trigger == "unknown_question":
            rules.append("- If you don't know the answer to a technical question → escalate")

    if not rules:
        rules = ["- If the lead explicitly asks to speak with a human → escalate immediately"]

    return "\n".join(rules)


def generate_system_prompt(
    business: Business,
    services: list[Service],
    service_zones: list[ServiceZone],
    escalation_rules: list,
    calendar_connected: bool = False,
    booking_url: Optional[str] = None,
    voice_mode: bool = False,
) -> str:
    lang = business.ai_language or "pt"
    hours_text = _format_hours(business.working_hours, lang)
    services_text = _format_services(services, lang)
    zone_text = _format_zone(service_zones, lang)
    escalation_text = _escalation_rules_text(escalation_rules, lang)
    booking_url = booking_url or business.booking_url

    language_instruction = ""
    if business.ai_detect_language:
        language_instruction = (
            "IMPORTANT: Detect the language the lead is writing in and always respond in the same language. "
            "If they write in Spanish, respond in Spanish. English → English. Portuguese → Portuguese."
        )
    else:
        language_instruction = f"Always respond in: {lang.upper()}"

    scheduling_instruction = ""
    if voice_mode:
        scheduling_instruction = (
            "SCHEDULING (VOICE CALL): You can book appointments directly during this call.\n"
            "Step 1 — Collect: lead's name, phone number, service needed, location, preferred date and time.\n"
            "Step 2 — Call check_availability({date, time}) to verify the slot is open.\n"
            "Step 3 — Once the lead confirms, call book_appointment({lead_name, phone, service, location, date, time}) "
            "to finalize the booking.\n"
            "Step 4 — Speak the confirmation back: 'Great, your appointment is confirmed for [date] at [time]!'\n"
            "NEVER say 'the team will confirm later' — book it right now on the call."
        )
    elif calendar_connected:
        scheduling_instruction = (
            "SCHEDULING: You have access to the provider's calendar. "
            "When the lead wants to schedule a visit, collect their preferred date and time, "
            "then confirm the appointment. Use the format: SCHEDULE_REQUEST:{\"date\":\"YYYY-MM-DD\",\"time\":\"HH:MM\",\"service\":\"...\",\"lead_name\":\"...\"}"
        )
    else:
        if booking_url:
            scheduling_instruction = (
                "SCHEDULING: When the lead confirms they want to schedule, "
                f"send them this booking link so they can pick a time: {booking_url} "
                "Say something like: 'Here is our booking link so you can choose your preferred time: [link]'"
            )
        else:
            scheduling_instruction = (
                "SCHEDULING: Calendar is not connected yet. "
                "Collect the lead's preferred date/time and inform the provider will confirm shortly."
            )

    prompt = f"""You are {business.ai_name}, the AI assistant for {business.name}.
Your role: qualify leads, answer questions about services, and schedule appointments.
Communication tone: {business.ai_tone}.

{language_instruction}

SERVICES OFFERED:
{services_text}

SERVICE AREA:
{zone_text}

WORKING HOURS ({business.timezone or 'local time'}):
{hours_text}

CONVERSATION RULES:
1. Always collect: lead's name, phone number, service needed, and location
2. Verify if the location is within the service area before proceeding
3. NEVER give exact prices — always give a range (e.g. "between $80 and $150")
4. If the lead is outside the service area, politely inform them and end the conversation
5. Be warm, professional, and concise — max 3 sentences per response
6. If you collect new info (name/phone/service/location), include it at the end of your response as:
   INFO:{{\"name\":\"...\",\"phone\":\"...\",\"service\":\"...\",\"location\":\"...\"}}
   Only include fields you just collected in this message.

{scheduling_instruction}

ESCALATION RULES (when to transfer to a human):
{escalation_text}
When escalating, include: ESCALATE:{{\"reason\":\"...\"}}

BUSINESS: {business.name} | AI ASSISTANT: {business.ai_name}
"""

    if business.welcome_message:
        prompt += f"\nWELCOME MESSAGE (use this for the first message): {business.welcome_message}"

    return prompt.strip()
