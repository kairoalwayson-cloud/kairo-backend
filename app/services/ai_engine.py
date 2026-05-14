"""
Kairo AI Engine
Calls Claude API with the dynamic system prompt and conversation history.
Parses structured actions (INFO, SCHEDULE_REQUEST, ESCALATE) from the response.
"""
import json
import re
import logging
from typing import Optional
from anthropic import Anthropic
from app.config import settings

logger = logging.getLogger(__name__)

# Max messages to include in context (older ones are trimmed to save tokens)
MAX_CONTEXT_MESSAGES = 20

# Claude model to use
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # fast + cheap for customer-facing chat


def _get_client() -> Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not configured")
    return Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _extract_action(text: str, tag: str) -> Optional[dict]:
    """Extract a tagged JSON payload from Claude's response text."""
    pattern = rf"{re.escape(tag)}(\{{.*?\}})"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse {tag} JSON: {match.group(1)}")
    return None


def _clean_response(text: str) -> str:
    """Remove structured tags from the visible response text."""
    text = re.sub(r"INFO:\{.*?\}", "", text, flags=re.DOTALL)
    text = re.sub(r"SCHEDULE_REQUEST:\{.*?\}", "", text, flags=re.DOTALL)
    text = re.sub(r"ESCALATE:\{.*?\}", "", text, flags=re.DOTALL)
    return text.strip()


class AIResponse:
    def __init__(
        self,
        message: str,
        raw: str,
        collected_info: Optional[dict] = None,
        schedule_request: Optional[dict] = None,
        escalate: Optional[dict] = None,
        tokens_used: int = 0,
    ):
        self.message = message          # clean text to send to lead
        self.raw = raw                  # full raw response from Claude
        self.collected_info = collected_info or {}    # {name, phone, service, location}
        self.schedule_request = schedule_request      # {date, time, service, lead_name}
        self.escalate = escalate                      # {reason}
        self.tokens_used = tokens_used

    @property
    def should_escalate(self) -> bool:
        return self.escalate is not None

    @property
    def has_schedule_request(self) -> bool:
        return self.schedule_request is not None


def call_claude(
    system_prompt: str,
    history: list[dict],   # [{"role": "user"|"assistant", "content": "..."}]
    user_message: str,
) -> AIResponse:
    """
    Call Claude API and return a parsed AIResponse.

    history: list of previous messages (already in Claude format)
    user_message: the new message from the lead
    """
    client = _get_client()

    # Trim history to max context
    trimmed_history = history[-MAX_CONTEXT_MESSAGES:] if len(history) > MAX_CONTEXT_MESSAGES else history

    messages = trimmed_history + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=messages,
    )

    raw_text = response.content[0].text
    tokens_used = response.usage.input_tokens + response.usage.output_tokens

    # Parse structured actions
    collected_info = _extract_action(raw_text, "INFO:")
    schedule_request = _extract_action(raw_text, "SCHEDULE_REQUEST:")
    escalate = _extract_action(raw_text, "ESCALATE:")

    # Clean message for the lead
    clean_message = _clean_response(raw_text)

    return AIResponse(
        message=clean_message,
        raw=raw_text,
        collected_info=collected_info,
        schedule_request=schedule_request,
        escalate=escalate,
        tokens_used=tokens_used,
    )


def detect_language(text: str) -> str:
    """Simple heuristic language detection before calling Claude."""
    text_lower = text.lower()

    # Spanish indicators
    es_words = ["hola", "necesito", "quiero", "ayuda", "como", "cuánto", "precio", "servicio", "gracias"]
    # Portuguese indicators
    pt_words = ["olá", "oi", "preciso", "quero", "ajuda", "como", "quanto", "preço", "serviço", "obrigado", "obrigada"]
    # English indicators
    en_words = ["hello", "hi", "need", "want", "help", "how much", "price", "service", "thank"]

    es_score = sum(1 for w in es_words if w in text_lower)
    pt_score = sum(1 for w in pt_words if w in text_lower)
    en_score = sum(1 for w in en_words if w in text_lower)

    scores = {"es": es_score, "pt": pt_score, "en": en_score}
    detected = max(scores, key=scores.get)

    # Default to pt if no clear signal
    if scores[detected] == 0:
        return "pt"

    return detected
