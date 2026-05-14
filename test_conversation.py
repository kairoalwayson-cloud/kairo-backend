"""
Test script — simulates a full conversation between a lead and the Kairo AI.
Creates a test business, then sends 4 messages as a lead.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal
from app.models.user import User
from app.models.business import Business
from app.models.service import Service
from app.models.service_zone import ServiceZone
from app.models.escalation_rule import EscalationRule
from app.core.security import hash_password
from app.services.conversation_service import process_message
import uuid

db = SessionLocal()

# --- Setup: create test business ---
print("Setting up test business...\n")

# Reuse existing user or create
user = db.query(User).filter(User.email == "carol@kairo.ai").first()
if not user:
    user = User(full_name="Carol Miranda", email="carol@kairo.ai", hashed_password=hash_password("Kairo2026!"))
    db.add(user)
    db.flush()

# Delete old test business if exists
old_biz = db.query(Business).filter(Business.name == "Limpeza Pro Test").first()
if old_biz:
    db.delete(old_biz)
    db.flush()

business = Business(
    owner_id=user.id,
    name="Limpeza Pro Test",
    owner_name="Carol Miranda",
    phone="+15551234567",
    city="Miami",
    country="US",
    ai_name="Ana",
    ai_gender="feminino",
    ai_tone="semi-formal",
    ai_language="en",
    ai_detect_language=True,
    welcome_message="Hi! I'm Ana, assistant for Limpeza Pro. How can I help you today?",
    working_hours={
        "mon": {"active": True, "start": "08:00", "end": "18:00"},
        "tue": {"active": True, "start": "08:00", "end": "18:00"},
        "wed": {"active": True, "start": "08:00", "end": "18:00"},
        "thu": {"active": True, "start": "08:00", "end": "18:00"},
        "fri": {"active": True, "start": "08:00", "end": "18:00"},
        "sat": {"active": True, "start": "08:00", "end": "12:00"},
        "sun": {"active": False},
    },
    timezone="America/New_York",
    plan="trial",
)
db.add(business)
db.flush()

# Services
services = [
    Service(business_id=business.id, name="Full Home Cleaning", description="All rooms, kitchen, bathrooms", price_min=80, price_max=200, price_unit="visit", active=True),
    Service(business_id=business.id, name="Post-Construction Cleaning", price_min=150, price_max=400, price_unit="visit", active=True),
    Service(business_id=business.id, name="Sofa Deep Clean", price_min=60, price_max=120, price_unit="piece", active=True),
]
for s in services:
    db.add(s)

# Service zone
zone = ServiceZone(
    business_id=business.id,
    type="radius",
    radius_km=30,
    center_lat=25.7617,
    center_lng=-80.1918,
)
db.add(zone)

# Escalation rules
rule = EscalationRule(
    business_id=business.id,
    trigger="explicit_request",
    action="notify_human",
    active=True,
)
db.add(rule)

db.commit()
print(f"Business ID: {business.id}")
print(f"AI Name: {business.ai_name}\n")
print("=" * 60)

# --- Simulate conversation ---
lead_phone = "+15559876543"

messages = [
    "Hi, I need help cleaning my house",
    "I'm in Brickell, Miami. It's a 3-bedroom apartment",
    "My name is John, can we do it this Saturday morning?",
    "How much would it cost?",
]

def safe_print(text):
    print(text.encode("ascii", errors="replace").decode("ascii"))

for msg in messages:
    safe_print(f"\n[LEAD]: {msg}")
    result = process_message(
        db=db,
        business_id=business.id,
        channel="whatsapp",
        sender_id=lead_phone,
        text=msg,
    )
    safe_print(f"[ANA]:  {result['response_text']}")
    if result.get("collected_info"):
        safe_print(f"  >> Collected: {result['collected_info']}")
    if result.get("escalate"):
        safe_print(f"  >> ESCALATE: {result['escalate']}")
    if result.get("schedule_request"):
        safe_print(f"  >> SCHEDULE: {result['schedule_request']}")
    safe_print(f"  >> Tokens: {result.get('tokens_used', '?')}")

print("\n" + "=" * 60)
print("Test complete!")
db.close()
