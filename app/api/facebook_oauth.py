"""
Facebook OAuth flow for Lead Ads integration.
Users connect their Facebook pages in one click — no manual tokens needed.
"""
import httpx
import urllib.parse
import uuid
import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.business import Business
from app.models.channel import Channel
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/facebook", tags=["facebook"])

GRAPH = "https://graph.facebook.com/v19.0"
FB_OAUTH = "https://www.facebook.com/v19.0/dialog/oauth"
BACKEND_URL = "https://backend-production-558d.up.railway.app"


def _redirect_uri() -> str:
    return f"{BACKEND_URL}/api/facebook/callback"


@router.get("/auth-url")
def get_auth_url(current_user: User = Depends(get_current_user)):
    """Return the Facebook OAuth URL — frontend redirects user here."""
    params = {
        "client_id": settings.FACEBOOK_APP_ID,
        "redirect_uri": _redirect_uri(),
        "scope": "pages_show_list,pages_manage_metadata",
        "response_type": "code",
        "state": str(current_user.id),
    }
    return {"url": f"{FB_OAUTH}?{urllib.parse.urlencode(params)}"}


@router.get("/callback")
async def facebook_callback(
    code: str = Query(...),
    state: str = Query(...),  # user_id passed through OAuth state
    db: Session = Depends(get_db),
):
    """Handle Facebook OAuth callback — exchange code, save pages, subscribe to leadgen."""
    error_url = f"{settings.FRONTEND_URL}/dashboard/settings?facebook=error"

    # Exchange code for short-lived user token
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH}/oauth/access_token", params={
            "client_id": settings.FACEBOOK_APP_ID,
            "client_secret": settings.FACEBOOK_APP_SECRET,
            "redirect_uri": _redirect_uri(),
            "code": code,
        })
        token_data = resp.json()

    if "error" in token_data:
        logger.error(f"Facebook token exchange error: {token_data}")
        return RedirectResponse(error_url)

    short_token = token_data["access_token"]

    # Exchange for long-lived token (60 days)
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.FACEBOOK_APP_ID,
            "client_secret": settings.FACEBOOK_APP_SECRET,
            "fb_exchange_token": short_token,
        })
        long_data = resp.json()

    long_token = long_data.get("access_token", short_token)

    # Get pages the user manages (each page has its own access token)
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH}/me/accounts", params={
            "access_token": long_token,
            "fields": "id,name,access_token",
        })
        pages_data = resp.json()

    pages = pages_data.get("data", [])
    if not pages:
        return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard/settings?facebook=no_pages")

    # Find business for this user
    user = db.query(User).filter(User.id == state).first()
    if not user:
        return RedirectResponse(error_url)

    business = db.query(Business).filter(Business.owner_id == user.id).first()
    if not business:
        return RedirectResponse(error_url)

    # Save each page and subscribe to leadgen events
    for page in pages:
        page_id = str(page["id"])
        page_name = page.get("name", page_id)
        page_token = page["access_token"]

        # Subscribe page to leadgen webhook events on our app
        try:
            async with httpx.AsyncClient() as client:
                sub = await client.post(
                    f"{GRAPH}/{page_id}/subscribed_apps",
                    params={"access_token": page_token, "subscribed_fields": "leadgen"},
                )
                logger.info(f"Page {page_id} ({page_name}) subscription: {sub.json()}")
        except Exception as e:
            logger.error(f"Failed to subscribe page {page_id}: {e}")

        # Upsert channel record
        existing = db.query(Channel).filter(
            Channel.business_id == business.id,
            Channel.identifier == page_id,
            Channel.provider == "meta",
        ).first()

        if existing:
            existing.config = {"access_token": page_token, "page_name": page_name}
            existing.active = True
        else:
            db.add(Channel(
                id=uuid.uuid4(),
                business_id=business.id,
                type="facebook_leads",
                provider="meta",
                identifier=page_id,
                active=True,
                config={"access_token": page_token, "page_name": page_name},
            ))

    db.commit()
    return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard/settings?facebook=connected")
