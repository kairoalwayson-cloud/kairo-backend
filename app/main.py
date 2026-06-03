from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.api import auth, conversations, webhooks, onboarding, calendar, vapi, billing, facebook_oauth, calls, phone
from app.api import settings as settings_router
from app.database import Base, engine

app = FastAPI(
    title=settings.APP_NAME,
    description="Kairo — AI assistant for service providers",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_URL,
        "http://localhost:3000",
        "http://localhost:19006",
        "https://web-ruby-five-31.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router, prefix="/api")
app.include_router(conversations.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")
app.include_router(onboarding.router)
app.include_router(calendar.router)
app.include_router(vapi.router)
app.include_router(settings_router.router)
app.include_router(billing.router)
app.include_router(facebook_oauth.router)
app.include_router(calls.router)
app.include_router(phone.router)


@app.post("/api/admin/relink-calls")
def relink_calls(secret: str, db=None):
    """One-time: link existing CallLogs to Leads by time proximity."""
    if secret != "kairo-relink-2026":
        from fastapi import HTTPException
        raise HTTPException(403, "forbidden")
    from app.database import get_db
    from app.models.call_log import CallLog
    from app.models.lead import Lead
    from sqlalchemy.orm import Session
    from datetime import timedelta
    db: Session = next(get_db())
    unlinked = db.query(CallLog).filter(CallLog.lead_id.is_(None)).all()
    updated = 0
    details = []
    for call in unlinked:
        if not call.business_id:
            continue
        # Find voice leads for this business created/updated near call end time
        ref_time = call.ended_at or call.created_at
        if not ref_time:
            continue
        window_start = ref_time - timedelta(minutes=15)
        window_end   = ref_time + timedelta(minutes=5)
        lead = db.query(Lead).filter(
            Lead.business_id == call.business_id,
            Lead.channel == "voice",
            Lead.status == "scheduled",
            Lead.last_message_at >= window_start,
            Lead.last_message_at <= window_end,
        ).order_by(Lead.last_message_at.desc()).first()
        if lead:
            call.lead_id = lead.id
            updated += 1
            details.append({"call_id": str(call.id), "lead_name": lead.name, "lead_id": str(lead.id)})
    db.commit()
    return {"updated": updated, "details": details, "total_unlinked": len(unlinked)}


@app.on_event("startup")
async def startup():
    import logging, asyncio
    try:
        import app.models.user, app.models.business, app.models.lead
        import app.models.conversation, app.models.channel
        import app.models.service, app.models.service_zone
        import app.models.calendar_integration, app.models.escalation_rule
        import app.models.call_log
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        logging.warning(f"DB startup create_all failed (non-fatal): {e}")

    _run_migrations()
    asyncio.create_task(_daily_report_task())


async def _daily_report_task():
    """Send daily WhatsApp digest to all businesses at 20:00 UTC every day."""
    import asyncio, logging
    from datetime import datetime, date
    last_sent: date | None = None

    while True:
        await asyncio.sleep(1800)  # check every 30 min
        now = datetime.utcnow()
        if now.hour == 20 and last_sent != now.date():
            last_sent = now.date()
            try:
                from app.database import SessionLocal
                from app.models.business import Business
                from app.services.notify_service import send_daily_report
                db = SessionLocal()
                businesses = db.query(Business).filter(
                    Business.is_active == True,
                    Business.notify_phone.isnot(None),
                ).all()
                for biz in businesses:
                    send_daily_report(biz, db)
                db.close()
            except Exception as e:
                logging.error(f"Daily report task error: {e}")


def _run_migrations():
    import logging
    migrations = [
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS booking_url VARCHAR",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS vapi_assistant_id VARCHAR",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS notify_on_new_lead BOOLEAN DEFAULT TRUE",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS notify_on_escalate BOOLEAN DEFAULT TRUE",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS notify_phone VARCHAR",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS quiet_hours_start INTEGER",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS quiet_hours_end INTEGER",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS plan VARCHAR DEFAULT 'trial'",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR",
        """CREATE TABLE IF NOT EXISTS call_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            lead_id UUID REFERENCES leads(id) ON DELETE SET NULL,
            vapi_call_id VARCHAR,
            caller_phone VARCHAR,
            duration_seconds INTEGER,
            status VARCHAR DEFAULT 'completed',
            transcript TEXT,
            messages_json JSONB,
            summary TEXT,
            recording_url VARCHAR,
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_call_logs_business_id ON call_logs(business_id)",
        "CREATE INDEX IF NOT EXISTS idx_call_logs_lead_id ON call_logs(lead_id)",
        "CREATE INDEX IF NOT EXISTS idx_call_logs_vapi_call_id ON call_logs(vapi_call_id)",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS twilio_phone_sid VARCHAR",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS twilio_phone_number VARCHAR",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS vapi_phone_id VARCHAR",
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS vapi_call_id VARCHAR",
        "CREATE INDEX IF NOT EXISTS idx_leads_vapi_call_id ON leads(vapi_call_id)",
    ]
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            for sql in migrations:
                conn.execute(text(sql))
            conn.commit()
    except Exception as e:
        logging.warning(f"Migration failed (non-fatal): {e}")

    # Backfill SMS channels for businesses that have a Vapi assistant but no SMS channel
    try:
        from sqlalchemy import text
        from app.services.vapi_service import PHONE_NUMBER_POOL
        twilio_number = next(iter(PHONE_NUMBER_POOL))
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO channels (id, business_id, type, provider, identifier, active, created_at)
                SELECT gen_random_uuid(), b.id, 'sms', 'twilio', :number, true, NOW()
                FROM businesses b
                WHERE b.vapi_assistant_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM channels c
                    WHERE c.business_id = b.id AND c.provider = 'twilio' AND c.type = 'sms'
                )
            """), {"number": twilio_number})
            conn.commit()
    except Exception as e:
        logging.warning(f"SMS channel backfill failed (non-fatal): {e}")


@app.get("/")
def root():
    return {"status": "ok", "app": settings.APP_NAME, "version": "0.1.0"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/debug-env")
def debug_env():
    import os
    db_url = os.environ.get("DATABASE_URL", "NOT_IN_ENV")
    settings_url = settings.DATABASE_URL
    masked = lambda u: u[:40] + "..." if len(u) > 40 else u
    return {
        "os_environ_DATABASE_URL": masked(db_url),
        "settings_DATABASE_URL": masked(settings_url),
        "match": db_url == settings_url,
    }
