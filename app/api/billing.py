"""
Stripe billing — subscriptions for Kairo plans (Starter/Pro/Scale).
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.models.business import Business

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])

PRICE_MAP = {
    "starter": lambda: settings.STRIPE_STARTER_PRICE_ID,
    "pro":     lambda: settings.STRIPE_PRO_PRICE_ID,
    "scale":   lambda: settings.STRIPE_SCALE_PRICE_ID,
}

PLAN_NAMES = {"starter": "Starter", "pro": "Pro", "scale": "Scale"}


def _stripe():
    import stripe as s
    s.api_key = settings.STRIPE_SECRET_KEY
    return s


def _get_or_create_customer(stripe, business: Business, user: User) -> str:
    if business.stripe_customer_id:
        return business.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email,
        name=business.owner_name or user.full_name,
        metadata={"business_id": str(business.id)},
    )
    return customer.id


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/status")
def billing_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        return {"plan": "trial", "status": "inactive", "publishable_key": settings.STRIPE_PUBLISHABLE_KEY}

    sub_status = "inactive"
    current_period_end = None

    if business.stripe_subscription_id and settings.STRIPE_SECRET_KEY:
        try:
            stripe = _stripe()
            sub = stripe.Subscription.retrieve(business.stripe_subscription_id)
            sub_status = sub.status
            current_period_end = sub.current_period_end
        except Exception as e:
            logger.error(f"Stripe subscription fetch error: {e}")

    return {
        "plan": business.plan or "trial",
        "status": sub_status,
        "stripe_customer_id": business.stripe_customer_id,
        "current_period_end": current_period_end,
        "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
    }


@router.post("/create-checkout")
def create_checkout(
    plan: str,
    trial_days: int = 0,
    return_to: str = "billing",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if plan not in PRICE_MAP:
        raise HTTPException(400, f"Invalid plan: {plan}")
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe not configured")

    price_id = PRICE_MAP[plan]()
    if not price_id:
        raise HTTPException(503, f"Price ID for {plan} not configured")

    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business:
        raise HTTPException(404, "Business not found")

    stripe = _stripe()
    customer_id = _get_or_create_customer(stripe, business, current_user)

    if business.stripe_customer_id != customer_id:
        business.stripe_customer_id = customer_id
        db.commit()

    success_path = "/dashboard?welcome=1" if return_to == "onboarding" else f"/dashboard/billing?success=1&plan={plan}"
    cancel_path  = "/onboarding" if return_to == "onboarding" else "/dashboard/billing?canceled=1"

    session_params: dict = {
        "customer": customer_id,
        "payment_method_types": ["card"],
        "line_items": [{"price": price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": f"{settings.FRONTEND_URL}{success_path}",
        "cancel_url": f"{settings.FRONTEND_URL}{cancel_path}",
        "metadata": {"business_id": str(business.id), "plan": plan},
    }
    if trial_days > 0:
        session_params["subscription_data"] = {"trial_period_days": trial_days}

    session = stripe.checkout.Session.create(**session_params)
    return {"checkout_url": session.url}


@router.post("/create-portal")
def create_portal(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Open Stripe Customer Portal to manage/cancel subscription."""
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe not configured")

    business = db.query(Business).filter(Business.owner_id == current_user.id).first()
    if not business or not business.stripe_customer_id:
        raise HTTPException(400, "No active subscription found")

    stripe = _stripe()
    session = stripe.billing_portal.Session.create(
        customer=business.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/dashboard/billing",
    )
    return {"portal_url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe sends events here — updates plan on successful payment."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    stripe = _stripe()

    if settings.STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig, settings.STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            logger.error(f"Webhook signature error: {e}")
            raise HTTPException(400, "Invalid signature")
    else:
        import json
        event = json.loads(payload)

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if event_type in ("checkout.session.completed", "invoice.paid"):
        business_id = data.get("metadata", {}).get("business_id")
        plan = data.get("metadata", {}).get("plan")
        subscription_id = data.get("subscription") or data.get("id")

        if business_id and plan:
            business = db.query(Business).filter(Business.id == business_id).first()
            if business:
                business.plan = plan
                if subscription_id and "sub_" in str(subscription_id):
                    business.stripe_subscription_id = subscription_id
                db.commit()
                logger.info(f"Plan updated: business={business_id} plan={plan}")

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        subscription_id = data.get("id")
        business = db.query(Business).filter(
            Business.stripe_subscription_id == subscription_id
        ).first()
        if business:
            business.plan = "trial"
            db.commit()
            logger.info(f"Plan reverted to trial: business={business.id}")

    return {"received": True}
