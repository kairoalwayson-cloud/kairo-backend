import base64
import hashlib
import os
import time

# Allow HTTP for local development (OAuth requires HTTPS by default)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from jose import jwt, JWTError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.calendar_integration import CalendarIntegration
from app.models.user import User

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

# ---------------------------------------------------------------------------
# Encryption helpers (Fernet key derived from SECRET_KEY)
# ---------------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CalendarEvent(BaseModel):
    id: str
    title: str
    start: str
    end: str
    all_day: bool
    provider: str
    color: str


class AvailabilitySlot(BaseModel):
    start: str
    end: str


class AvailabilityResponse(BaseModel):
    date: str
    busy: List[AvailabilitySlot]
    available: List[AvailabilitySlot]


class AppleConnectRequest(BaseModel):
    apple_id: str
    app_password: str


class StatusResponse(BaseModel):
    google: bool
    apple: bool


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get("/debug-config")
def debug_config():
    client_id = settings.GOOGLE_CLIENT_ID or os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = settings.GOOGLE_CLIENT_SECRET or os.getenv("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = settings.GOOGLE_REDIRECT_URI or os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8003/api/calendar/google/callback")
    return {
        "client_id_ok": bool(client_id),
        "client_secret_ok": bool(client_secret),
        "redirect_uri": redirect_uri,
    }


@router.get("/status", response_model=StatusResponse)
def calendar_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    integrations = db.query(CalendarIntegration).filter_by(
        user_id=current_user.id, is_active=True
    ).all()
    providers = {i.provider for i in integrations}
    return {"google": "google" in providers, "apple": "apple" in providers}


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _build_flow():
    from google_auth_oauthlib.flow import Flow
    client_id = settings.GOOGLE_CLIENT_ID or os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = settings.GOOGLE_CLIENT_SECRET or os.getenv("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = settings.GOOGLE_REDIRECT_URI or os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8003/api/calendar/google/callback")
    if not client_id or not client_secret:
        raise HTTPException(400, "Google Calendar not configured. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env")
    return Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )


@router.get("/google/auth-url")
def google_auth_url(
    return_to: str = "agenda",
    mobile: bool = False,
    current_user: User = Depends(get_current_user),
):
    flow = _build_flow()
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    # Store code_verifier in state so callback can use it
    state_data: dict = {"user_id": str(current_user.id), "exp": time.time() + 600, "return_to": return_to, "mobile": mobile}
    if getattr(flow, "code_verifier", None):
        state_data["cv"] = flow.code_verifier
    state = jwt.encode(state_data, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    # Replace the auto-generated state with our signed one
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["state"] = [state]
    url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
    return {"url": url}


@router.get("/google/callback")
def google_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = payload["user_id"]
    except JWTError:
        return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard/agenda?error=invalid_state")

    try:
        flow = _build_flow()
        code_verifier = payload.get("cv")
        if code_verifier:
            flow.fetch_token(code=code, code_verifier=code_verifier)
        else:
            flow.fetch_token(code=code)
    except Exception as exc:
        return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard/agenda?error={str(exc)[:120]}")

    creds = flow.credentials

    existing = db.query(CalendarIntegration).filter_by(user_id=user_id, provider="google").first()
    if existing:
        existing.access_token = _encrypt(creds.token)
        existing.refresh_token = _encrypt(creds.refresh_token) if creds.refresh_token else existing.refresh_token
        existing.token_expiry = creds.expiry
        existing.is_active = True
    else:
        integration = CalendarIntegration(
            user_id=user_id,
            provider="google",
            access_token=_encrypt(creds.token),
            refresh_token=_encrypt(creds.refresh_token) if creds.refresh_token else None,
            token_expiry=creds.expiry,
        )
        db.add(integration)

    db.commit()
    return_to = payload.get("return_to", "agenda")
    mobile = payload.get("mobile", False)
    if mobile:
        return RedirectResponse("kairo://agenda?connected=google")
    if return_to == "onboarding":
        return RedirectResponse(f"{settings.FRONTEND_URL}/onboarding?connected=google")
    return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard/agenda?connected=google")


@router.delete("/google/disconnect", status_code=204)
def google_disconnect(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(CalendarIntegration).filter_by(
        user_id=current_user.id, provider="google"
    ).update({"is_active": False})
    db.commit()


# ---------------------------------------------------------------------------
# Apple CalDAV
# ---------------------------------------------------------------------------

APPLE_CALDAV_URL = "https://caldav.icloud.com"


@router.post("/apple/connect")
def apple_connect(
    body: AppleConnectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        import caldav
        client = caldav.DAVClient(
            url=APPLE_CALDAV_URL,
            username=body.apple_id,
            password=body.app_password,
            ssl_verify_cert=True,
        )
        # Test the connection — this will raise if credentials are wrong
        principal = client.principal()
        _ = principal.calendars()
    except Exception as exc:
        raise HTTPException(400, f"Could not connect to Apple Calendar: {exc}")

    existing = db.query(CalendarIntegration).filter_by(
        user_id=current_user.id, provider="apple"
    ).first()
    if existing:
        existing.caldav_url = APPLE_CALDAV_URL
        existing.caldav_username = body.apple_id
        existing.caldav_password = _encrypt(body.app_password)
        existing.is_active = True
    else:
        integration = CalendarIntegration(
            user_id=current_user.id,
            provider="apple",
            caldav_url=APPLE_CALDAV_URL,
            caldav_username=body.apple_id,
            caldav_password=_encrypt(body.app_password),
        )
        db.add(integration)

    db.commit()
    return {"success": True, "message": "Apple Calendar connected successfully"}


@router.delete("/apple/disconnect", status_code=204)
def apple_disconnect(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(CalendarIntegration).filter_by(
        user_id=current_user.id, provider="apple"
    ).update({"is_active": False})
    db.commit()


# ---------------------------------------------------------------------------
# Events (unified)
# ---------------------------------------------------------------------------

def _get_google_service(integration: CalendarIntegration):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials(
        token=_decrypt(integration.access_token),
        refresh_token=_decrypt(integration.refresh_token) if integration.refresh_token else None,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _google_events(integration: CalendarIntegration, start_dt: datetime, end_dt: datetime) -> List[CalendarEvent]:
    service = _get_google_service(integration)
    result = service.events().list(
        calendarId="primary",
        timeMin=start_dt.isoformat() + "Z",
        timeMax=end_dt.isoformat() + "Z",
        singleEvents=True,
        orderBy="startTime",
        maxResults=250,
    ).execute()

    events = []
    for item in result.get("items", []):
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})
        all_day = "date" in start_raw and "dateTime" not in start_raw
        start_str = start_raw.get("dateTime") or (start_raw.get("date") + "T00:00:00")
        end_str = end_raw.get("dateTime") or (end_raw.get("date") + "T00:00:00")
        events.append(CalendarEvent(
            id=item["id"],
            title=item.get("summary", "Untitled"),
            start=start_str,
            end=end_str,
            all_day=all_day,
            provider="google",
            color="#4285F4",
        ))
    return events


def _apple_events(integration: CalendarIntegration, start_dt: datetime, end_dt: datetime) -> List[CalendarEvent]:
    import caldav

    password = _decrypt(integration.caldav_password)
    client = caldav.DAVClient(
        url=integration.caldav_url,
        username=integration.caldav_username,
        password=password,
        ssl_verify_cert=True,
    )
    principal = client.principal()
    calendars = principal.calendars()

    events = []
    for cal in calendars:
        try:
            raw_events = cal.date_search(start=start_dt, end=end_dt, expand=True)
        except Exception:
            continue
        for ev in raw_events:
            try:
                vevent = ev.vobject_instance.vevent
                title = str(vevent.summary.value) if hasattr(vevent, "summary") else "Untitled"
                dtstart = vevent.dtstart.value
                dtend = vevent.dtend.value if hasattr(vevent, "dtend") else dtstart

                all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)
                start_str = dtstart.isoformat() if all_day else _to_iso(dtstart)
                end_str = dtend.isoformat() if isinstance(dtend, date) and not isinstance(dtend, datetime) else _to_iso(dtend)

                events.append(CalendarEvent(
                    id=str(ev.url) if ev.url else title + start_str,
                    title=title,
                    start=start_str + "T00:00:00" if all_day else start_str,
                    end=end_str + "T00:00:00" if all_day and "T" not in end_str else end_str,
                    all_day=all_day,
                    provider="apple",
                    color="#1C1C1E",
                ))
            except Exception:
                continue
    return events


def _to_iso(dt) -> str:
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


@router.get("/events", response_model=List[CalendarEvent])
def get_events(
    start: str = Query(..., description="ISO date, e.g. 2026-04-01"),
    end: str = Query(..., description="ISO date, e.g. 2026-04-30"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    start_dt = datetime.fromisoformat(start).replace(tzinfo=None)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=None) + timedelta(days=1)

    integrations = db.query(CalendarIntegration).filter_by(
        user_id=current_user.id, is_active=True
    ).all()

    all_events: List[CalendarEvent] = []
    for integration in integrations:
        try:
            if integration.provider == "google":
                all_events.extend(_google_events(integration, start_dt, end_dt))
            elif integration.provider == "apple":
                all_events.extend(_apple_events(integration, start_dt, end_dt))
        except Exception as exc:
            # Log but don't fail the whole request
            print(f"[calendar] Error fetching {integration.provider} events: {exc}")

    all_events.sort(key=lambda e: e.start)
    return all_events


# ---------------------------------------------------------------------------
# Availability (free/busy for a single day)
# ---------------------------------------------------------------------------

@router.get("/availability", response_model=AvailabilityResponse)
def get_availability(
    date_str: str = Query(..., alias="date", description="ISO date, e.g. 2026-04-16"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    start_dt = datetime.fromisoformat(date_str)
    end_dt = start_dt + timedelta(days=1)

    integrations = db.query(CalendarIntegration).filter_by(
        user_id=current_user.id, is_active=True
    ).all()

    busy_slots: List[AvailabilitySlot] = []
    for integration in integrations:
        try:
            if integration.provider == "google":
                events = _google_events(integration, start_dt, end_dt)
            elif integration.provider == "apple":
                events = _apple_events(integration, start_dt, end_dt)
            else:
                continue
            for ev in events:
                if not ev.all_day:
                    busy_slots.append(AvailabilitySlot(start=ev.start, end=ev.end))
        except Exception as exc:
            print(f"[calendar] Availability error for {integration.provider}: {exc}")

    # Build available slots between 08:00 and 18:00 excluding busy periods
    work_start = start_dt.replace(hour=8, minute=0, second=0)
    work_end = start_dt.replace(hour=18, minute=0, second=0)
    available = _compute_free_slots(work_start, work_end, busy_slots)

    return AvailabilityResponse(
        date=date_str,
        busy=busy_slots,
        available=available,
    )


def _compute_free_slots(
    work_start: datetime,
    work_end: datetime,
    busy: List[AvailabilitySlot],
) -> List[AvailabilitySlot]:
    if not busy:
        return [AvailabilitySlot(start=work_start.isoformat(), end=work_end.isoformat())]

    busy_dt = sorted(
        [(datetime.fromisoformat(b.start.replace("Z", "")), datetime.fromisoformat(b.end.replace("Z", "")))
         for b in busy],
        key=lambda x: x[0],
    )

    free = []
    cursor = work_start
    for bs, be in busy_dt:
        bs = max(bs, work_start)
        be = min(be, work_end)
        if cursor < bs:
            free.append(AvailabilitySlot(start=cursor.isoformat(), end=bs.isoformat()))
        cursor = max(cursor, be)

    if cursor < work_end:
        free.append(AvailabilitySlot(start=cursor.isoformat(), end=work_end.isoformat()))

    return free


# ---------------------------------------------------------------------------
# Helpers for Vapi tool calls
# ---------------------------------------------------------------------------

def get_day_availability(user_id: str, date_str: str, db) -> list:
    """
    Returns free AvailabilitySlot windows for the given day (08:00–18:00).
    Used by Vapi's get_available_slots tool call.
    Falls back to full working hours if no calendar is connected or check fails.
    """
    from app.models.calendar_integration import CalendarIntegration

    start_dt = datetime.strptime(date_str, "%Y-%m-%d")
    end_dt = start_dt + timedelta(days=1)
    work_start = start_dt.replace(hour=8, minute=0, second=0)
    work_end = start_dt.replace(hour=18, minute=0, second=0)

    integration = db.query(CalendarIntegration).filter_by(
        user_id=user_id, provider="google", is_active=True
    ).first()

    if not integration:
        return [AvailabilitySlot(start=work_start.isoformat(), end=work_end.isoformat())]

    try:
        events = _google_events(integration, start_dt, end_dt)
        busy_slots = [AvailabilitySlot(start=ev.start, end=ev.end) for ev in events if not ev.all_day]
        return _compute_free_slots(work_start, work_end, busy_slots)
    except Exception:
        return [AvailabilitySlot(start=work_start.isoformat(), end=work_end.isoformat())]


def is_slot_available(user_id: str, date_str: str, time_str: str, db) -> bool:
    """
    Returns True if the given slot is free in the user's Google Calendar.
    Falls back to True if no calendar is connected or if the check fails.
    """
    integration = db.query(CalendarIntegration).filter_by(
        user_id=user_id, provider="google", is_active=True
    ).first()
    if not integration:
        return True

    try:
        slot_start = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        slot_end = slot_start + timedelta(hours=1)
        events = _google_events(integration, slot_start - timedelta(minutes=1), slot_end + timedelta(minutes=1))
        for ev in events:
            if ev.all_day:
                continue
            ev_start = datetime.fromisoformat(ev.start.replace("Z", ""))
            ev_end = datetime.fromisoformat(ev.end.replace("Z", ""))
            if ev_start < slot_end and ev_end > slot_start:
                return False
        return True
    except Exception:
        return True


def create_appointment_event(
    user_id: str,
    lead_name: str,
    service: str,
    location: str,
    start_dt: datetime,
    db,
) -> Optional[str]:
    """
    Creates a Google Calendar event for a confirmed appointment.
    Returns the event HTML link, or None if calendar is not connected / fails.
    """
    integration = db.query(CalendarIntegration).filter_by(
        user_id=user_id, provider="google", is_active=True
    ).first()
    if not integration:
        return None

    try:
        # start_dt is already UTC (converted by _parse_appointment_dt with pytz)
        gcal = _get_google_service(integration)
        end_dt = start_dt + timedelta(hours=1)
        event = {
            "summary": f"{service} — {lead_name}",
            "description": f"Client: {lead_name}\nService: {service}\nLocation: {location or '—'}\n\nBooked via Kairo AI",
            "location": location or "",
            "start": {"dateTime": start_dt.isoformat() + "Z", "timeZone": "UTC"},
            "end": {"dateTime": end_dt.isoformat() + "Z", "timeZone": "UTC"},
        }
        result = gcal.events().insert(calendarId="primary", body=event).execute()
        return result.get("id")  # return event ID so it can be saved on the lead
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Create calendar event failed: {e}")
        return None
