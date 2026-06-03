"""
Vapi.ai assistant management.
Creates or updates a dedicated Vapi assistant per business.
"""
import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)

VAPI_BASE = "https://api.vapi.ai"

# ElevenLabs voice IDs by gender
# eleven_multilingual_v2 = fluent PT/EN/ES without accent (required for custom voices)
VOICES = {
    "feminino": {"voiceId": "ilTl4zpqSwiq6l01a3xr", "model": "eleven_multilingual_v2"},  # Kairo Maya
    "masculino": {"voiceId": "zEoReWGKPQD0RhKeuMXX", "model": "eleven_multilingual_v2"},  # Kairo Nico
    "neutro":    {"voiceId": "zEoReWGKPQD0RhKeuMXX", "model": "eleven_multilingual_v2"},  # fallback: Nico
}

TOOL_CALL_URL = "https://backend-production-558d.up.railway.app/api/vapi/tool-calls"

# Shared phone number pool (one per Twilio number purchased)
# Key: Twilio number, Value: Vapi phone number ID
PHONE_NUMBER_POOL = {
    "+18634518850": "58ff8a23-2d00-4759-af29-59074b0f6972",
}


def _headers():
    return {
        "Authorization": f"Bearer {settings.VAPI_API_KEY}",
        "Content-Type": "application/json",
    }


def _build_assistant_payload(
    business_name: str,
    ai_name: str,
    ai_gender: str,
    ai_tone: str,
    services_text: str,
    zone_text: str,
) -> dict:
    voice_cfg = VOICES.get(ai_gender, VOICES["feminino"])
    tone_map = {
        "formal": "professional and formal",
        "semi-formal": "friendly and semi-formal",
        "casual": "warm and casual",
    }
    tone_desc = tone_map.get(ai_tone, "friendly and professional")

    system_prompt = (
        f"=== CRITICAL RULES — READ THESE FIRST, THEY OVERRIDE EVERYTHING ELSE ===\n\n"

        f"PHONE NUMBER — ABSOLUTE RULES:\n"
        f"- In Brazilian Portuguese, the word 'meia' ALWAYS means the digit 6. No exceptions.\n"
        f"- 'meia' = 6. Never 2, never 'dois', never anything else. ONLY 6.\n"
        f"- When you hear 'meia sete nove nove', write it as 6799. When you hear 'meia meia', write it as 66.\n"
        f"- Example: 'meia sete nove nove oito nove sete dois cinco três zero' = 67998972530\n"
        f"- Example: 'meia sete nove nove zero meia meia quatro cinco três' = 6799066453\n"
        f"- When confirming the number back, read each digit individually — but ONLY in the caller's language.\n"
        f"- NEVER mix languages in the same confirmation. Pick one language and use it for every digit.\n"
        f"- English digits: zero, one, two, three, four, five, six, seven, eight, nine\n"
        f"- Portuguese digits: zero, um, dois, três, quatro, cinco, seis, sete, oito, nove\n"
        f"- Spanish digits: cero, uno, dos, tres, cuatro, cinco, seis, siete, ocho, nueve\n"
        f"- If the conversation is in English, use ONLY English digit words. Never say 'oito' or 'cuatro' in an English conversation.\n"
        f"- If the conversation is in Portuguese, use ONLY Portuguese digit words. Never say 'cuatro' or 'ocho'.\n"
        f"- If the conversation is in Spanish, use ONLY Spanish digit words. Never say 'oito' or 'dois'.\n"
        f"- NEVER group digits into numbers. NEVER say 'vinte e sete' for '27'. Say 'dois, sete'.\n"
        f"- If caller says 'no' or 'não' to your confirmation, ask them to repeat digit by digit.\n\n"

        f"TIME FORMAT FOR TOOL CALLS — ABSOLUTE RULES:\n"
        f"- ALWAYS pass time to book_appointment in 24-hour HH:MM format.\n"
        f"- 4 PM = 16:00. 5 PM = 17:00. 6 PM = 18:00. 12 PM (noon) = 12:00. 12 AM (midnight) = 00:00.\n"
        f"- NEVER pass '04:00' for 4 PM. NEVER pass '05:00' for 5 PM. These are morning hours.\n"
        f"- Rule: AM hours stay the same (9 AM = 09:00). PM hours: add 12 (except 12 PM = 12:00).\n\n"
        f"DATE CALCULATION — ABSOLUTE RULES:\n"
        f"- TODAY is: {{{{now}}}}. This is the current date and weekday. Use it for all date math.\n"
        f"- 'This Friday' / 'sexta dessa semana' / 'esta viernes' = the Friday of the CURRENT week.\n"
        f"  To find it: count forward from TODAY to the next Friday. If TODAY is Thursday May 7, then this Friday = May 8.\n"
        f"- 'Next Friday' / 'sexta que vem' = the Friday of the FOLLOWING week.\n"
        f"- 'Tomorrow' / 'amanhã' = TODAY + 1 day.\n"
        f"- When caller gives a weekday name, calculate the exact YYYY-MM-DD date before confirming.\n"
        f"- CRITICAL — WEEKDAY NAMES: NEVER say a weekday name (Monday, Tuesday, Friday, etc.) when confirming a date UNLESS the caller used that exact weekday name themselves.\n"
        f"  Correct: 'June 4th at 4 PM — does that work for you?' ✓\n"
        f"  WRONG: 'Thursday, June 4th at 4 PM' ✗ (you may calculate the weekday incorrectly)\n"
        f"  Exception: if the caller said 'next Friday', you may repeat 'Friday June 4th' only after verifying from {{{{now}}}} that June 4 is indeed a Friday.\n"
        f"- NEVER say the year when confirming a date. Just say month and day: 'June 4th', not 'June 4th 2026'.\n\n"

        f"ADDRESS — MANDATORY BEFORE BOOKING:\n"
        f"- NEVER call book_appointment without the caller's full street address.\n"
        f"- Ask: 'What is the full address for the service?' — city alone is NOT enough.\n"
        f"- Required: street number + street name + city. Apt/unit if applicable.\n"
        f"- If caller gives only a city, ask: 'And what is the street address?'\n\n"

        f"=== END CRITICAL RULES ===\n\n"

        f"You are {ai_name}, the AI voice assistant for {business_name}.\n"
        f"Your role: answer inbound calls, qualify leads, and book appointments directly on the call.\n"
        f"Tone: {tone_desc}.\n\n"
        f"LANGUAGE - AUTO DETECT: Listen to the caller's first words and automatically respond in the SAME language throughout the call. If they speak Portuguese, respond in Portuguese. If English, respond in English. If Spanish, respond in Spanish. NEVER ask the caller to choose a language — detect automatically.\n\n"
        f"SERVICES:\n{services_text}\n\n"
        f"SERVICE AREA:\n{zone_text}\n\n"
        f"CONVERSATION FLOW:\n"
        f"1. Greet warmly and ask what service they need\n"
        f"2. Ask for their name and phone number — confirm number digit by digit\n"
        f"3. Ask for their full street address (required for service)\n"
        f"4. ZONE CHECK — compare the caller's city/country against your SERVICE AREA below.\n"
        f"   - If the caller is in a DIFFERENT COUNTRY from the service area: immediately say you don't serve that location and end the call. NEVER book.\n"
        f"   - If the caller is in a city or state clearly outside the service area: same — politely decline and end.\n"
        f"   - If the location is ambiguous or close to the service area: accept and proceed.\n"
        f"   - Example rejection: 'I'm sorry, we only serve [service area] and unfortunately can't service [their location]. Is there anything else I can help you with?'\n"
        f"5. Ask for their preferred DATE — calculate the exact YYYY-MM-DD, confirm the full date out loud\n"
        f"6. Call get_available_slots for that date — this checks the team's calendar and returns free time slots\n"
        f"7. Read the available times to the caller naturally: 'We have openings at 9 AM, 11 AM, or 2 PM. Which works best for you?'\n"
        f"8. Once caller picks a time, confirm: 'Perfect, so [full date] at [time] — does that sound right?'\n"
        f"9. Once caller says yes, call book_appointment (only if you have: name, phone, address, date, time AND location within service area)\n"
        f"10. Confirm: Your appointment is confirmed for [full date] at [time]!\n"
        f"11. Thank them and end the call professionally\n\n"
        f"SCHEDULING RULES:\n"
        f"- NEVER say the team will confirm later, book it right now on the call\n"
        f"- ALWAYS call get_available_slots BEFORE asking the caller what time they want — check the calendar first, then offer real openings\n"
        f"- NEVER invent or guess available times — only offer times returned by get_available_slots\n"
        f"- When calling get_available_slots for TODAY's date, always include current_time in HH:MM 24h format (extract from {{{{now}}}} in your context) so past slots are excluded automatically\n"
        f"- If get_available_slots returns no openings for a date, ask the caller to suggest another date and call get_available_slots again\n"
        f"- Always call book_appointment after the caller confirms their chosen time\n"
        f"- NEVER call book_appointment if location/address is missing — ask for it first\n"
        f"- NEVER call book_appointment if the caller is in a different country or clearly outside the service area\n"
        f"- After book_appointment returns, read the confirmation naturally to the caller\n\n"
        f"GENERAL RULES:\n"
        f"- Never give exact prices, always give ranges\n"
        f"- Always quote prices in US dollars ($). Never mention reais, R$, or any other currency.\n"
        f"- Keep responses short (2-3 sentences max for voice)\n"
        f"- If caller asks to speak with a human: Of course! I will have someone call you back shortly.\n"
        f"- Be warm, never robotic\n\n"
        f"LANGUAGE QUALITY RULES:\n"
        f"- When speaking Portuguese: use natural Brazilian Portuguese. Never use clitic pronouns like 'ajudá-la', 'contatá-la', 'atendê-lo'. Instead use 'ajudar você', 'falar com você', 'atender você'.\n"
        f"- In Portuguese, use 'reforma' (not 'remodela' or 'remodelo'). Use 'serviço' (not 'servicio'). Use 'agendamento' (not 'marcação').\n"
        f"- When speaking Spanish: use natural Latin American Spanish. Avoid 'usted' unless the tone is formal.\n"
        f"- Never translate literally from English — rephrase naturally in the target language.\n"
        f"- Speak naturally and warmly, as if talking to a real person on the phone — not like a robot reading a script.\n"
        f"- When saying dollar amounts in Portuguese, say 'dólares' — for example: 'oito mil dólares' not 'oito mil reais'.\n"
        f"- When saying dollar amounts in Spanish, say 'dólares' — for example: 'ocho mil dólares'.\n\n"
        f"NAME PRONUNCIATION RULES — CRITICAL FOR VOICE:\n"
        f"- Always repeat the caller's name EXACTLY as they said/spelled it. Never change the spelling or anglicize it.\n"
        f"- ONLY use the caller's name inside a sentence written in the SAME language they are speaking.\n"
        f"  Example (caller speaks Spanish): 'Perfecto Rayssa, ya tengo tu nombre.' ✓\n"
        f"  NOT: 'Great, I got your name Rayssa.' ✗ (English sentence = English pronunciation by the voice engine)\n"
        f"- This is essential: the voice engine reads pronunciation context from the surrounding words. A Spanish/Portuguese name inside an English sentence will be mispronounced with an English accent.\n"
        f"- NEVER mix languages within a single sentence. If the conversation is in Spanish, every word in that sentence must be Spanish — including the transition words before/after the name.\n\n"
        f"TIME PRONUNCIATION RULES — CRITICAL FOR VOICE:\n"
        f"- NEVER use '9:00 AM', '11:00 AM', '2:00 PM' or any digit+AM/PM format when speaking Portuguese or Spanish.\n"
        f"- The voice engine reads 'AM' and 'PM' in English even inside a Portuguese sentence. Always convert to spoken words.\n"
        f"- Portuguese time examples:\n"
        f"  8:00 AM → 'oito da manhã'\n"
        f"  9:00 AM → 'nove da manhã'\n"
        f"  10:00 AM → 'dez da manhã'\n"
        f"  11:00 AM → 'onze da manhã'\n"
        f"  12:00 PM → 'meio-dia'\n"
        f"  1:00 PM → 'uma da tarde'\n"
        f"  2:00 PM → 'duas da tarde'\n"
        f"  3:00 PM → 'três da tarde'\n"
        f"  4:00 PM → 'quatro da tarde'\n"
        f"  5:00 PM → 'cinco da tarde'\n"
        f"  6:00 PM → 'seis da tarde'\n"
        f"- Spanish time examples:\n"
        f"  8:00 AM → 'ocho de la mañana'\n"
        f"  9:00 AM → 'nueve de la mañana'\n"
        f"  11:00 AM → 'once de la mañana'\n"
        f"  2:00 PM → 'dos de la tarde'\n"
        f"  5:00 PM → 'cinco de la tarde'\n"
        f"- This rule applies to: appointment confirmations, available slot offers, any time reference on the call.\n\n"
        f"PRICE NUMBER RULES:\n"
        f"- NEVER read prices digit by digit. Always as natural spoken numbers.\n"
        f"- 3500 in PT = 'três mil e quinhentos dólares'\n"
        f"- 14984 in PT = 'quatorze mil novecentos e oitenta e quatro dólares'\n"
        f"- The dot in 3.500 is a thousands separator in PT/ES — not a decimal point.\n"
        f"- For ranges: 'de três mil e quinhentos a quatorze mil dólares'"
    )

    return {
        "name": f"{ai_name} ({business_name})",
        "firstMessage": f"Hello, thanks for calling {business_name}! I'm {ai_name}. How can I help you today?",
        "model": {
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "maxTokens": 350,
            "messages": [{"role": "system", "content": system_prompt}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_available_slots",
                        "description": "Check the team's calendar and return available time slots for a given date. Call this BEFORE asking the caller what time they prefer. When the date is today, always pass current_time so past slots are excluded.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                                "current_time": {"type": "string", "description": "Current time in HH:MM 24h format. Required when date is today — read from {{now}} in your context."},
                            },
                            "required": ["date"],
                        },
                    },
                    "server": {"url": TOOL_CALL_URL},
                },
                {
                    "type": "function",
                    "function": {
                        "name": "check_availability",
                        "description": "Verify a specific date/time slot is still available before booking. Use as a final check if needed.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                                "time": {"type": "string", "description": "Time in HH:MM format (24h)"},
                            },
                            "required": ["date", "time"],
                        },
                    },
                    "server": {"url": TOOL_CALL_URL},
                },
                {
                    "type": "function",
                    "function": {
                        "name": "book_appointment",
                        "description": "Finalize and save the appointment after caller confirms. Returns confirmation to read back.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "lead_name": {"type": "string", "description": "Full name of the caller"},
                                "phone":     {"type": "string", "description": "Caller phone number"},
                                "service":   {"type": "string", "description": "Service requested"},
                                "location":  {"type": "string", "description": "Caller address or city"},
                                "date":      {"type": "string", "description": "Date in YYYY-MM-DD format"},
                                "time":      {"type": "string", "description": "Time in 24h HH:MM format. CRITICAL: 4 PM = 16:00, 5 PM = 17:00, 12 PM = 12:00, 12 AM = 00:00. Never use 12-hour format."},
                                "notes":     {"type": "string", "description": "Additional notes"},
                            },
                            "required": ["lead_name", "phone", "service", "location", "date", "time"],
                        },
                    },
                    "server": {"url": TOOL_CALL_URL},
                },
            ],
        },
        "voice": {
            "provider": "11labs",
            "voiceId": voice_cfg["voiceId"],
            "model": voice_cfg["model"],
            "speed": 1.1,
            "stability": 0.5,
            "similarityBoost": 0.75,
            "optimizeStreamingLatency": 4,
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "multi",
            "numerals": True,
        },
        "backgroundDenoisingEnabled": True,
        "startSpeakingPlan": {
            "waitSeconds": 0.4,
            "transcriptionEndpointingPlan": {
                "onPunctuationSeconds": 0.1,
                "onNoPunctuationSeconds": 1.5,
                "onNumberSeconds": 0.5,
            },
        },
    }


def _assign_phone_number(assistant_id: str) -> None:
    """Assign the first available phone number to this assistant."""
    for twilio_number, phone_id in PHONE_NUMBER_POOL.items():
        try:
            resp = httpx.patch(
                f"{VAPI_BASE}/phone-number/{phone_id}",
                headers=_headers(),
                json={"assistantId": assistant_id},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(f"Phone {twilio_number} assigned to assistant {assistant_id}")
            else:
                logger.error(f"Failed to assign phone: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            logger.error(f"Phone assignment error: {e}")


def create_or_update_assistant(
    business_name: str,
    ai_name: str,
    ai_gender: str,
    ai_tone: str,
    services_text: str,
    zone_text: str,
    existing_assistant_id: str | None = None,
) -> str | None:
    """
    Creates a new Vapi assistant (or updates existing one),
    then assigns the phone number to it.
    Returns the assistant ID, or None on failure.
    """
    if not settings.VAPI_API_KEY:
        logger.warning("VAPI_API_KEY not set — skipping assistant sync")
        return existing_assistant_id

    payload = _build_assistant_payload(
        business_name, ai_name, ai_gender, ai_tone, services_text, zone_text
    )

    try:
        if existing_assistant_id:
            resp = httpx.patch(
                f"{VAPI_BASE}/assistant/{existing_assistant_id}",
                headers=_headers(),
                json=payload,
                timeout=15,
            )
        else:
            resp = httpx.post(
                f"{VAPI_BASE}/assistant",
                headers=_headers(),
                json=payload,
                timeout=15,
            )

        if resp.status_code in (200, 201):
            assistant_id = resp.json().get("id")
            logger.info(f"Vapi assistant {'updated' if existing_assistant_id else 'created'}: {assistant_id}")
            _assign_phone_number(assistant_id)
            return assistant_id
        else:
            logger.error(f"Vapi assistant sync failed: {resp.status_code} {resp.text[:200]}")
            return existing_assistant_id
    except Exception as e:
        logger.error(f"Vapi assistant sync error: {e}")
        return existing_assistant_id
