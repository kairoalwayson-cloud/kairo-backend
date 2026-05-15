import logging
import smtplib
import httpx
from email.mime.text import MIMEText
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.core.security import hash_password, verify_password, create_access_token, create_reset_token, decode_reset_token
from app.core.deps import get_current_user
from app.schemas.auth import RegisterRequest, LoginRequest, TokenResponse, UserResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    # Check duplicate email
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = User(
        full_name=payload.full_name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        onboarding_step="phone",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(subject=str(user.id))
    return TokenResponse(
        access_token=token,
        user_id=str(user.id),
        full_name=user.full_name,
        onboarding_completed=user.onboarding_completed,
        onboarding_step=user.onboarding_step,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive",
        )

    token = create_access_token(subject=str(user.id))
    return TokenResponse(
        access_token=token,
        user_id=str(user.id),
        full_name=user.full_name,
        onboarding_completed=user.onboarding_completed,
        onboarding_step=user.onboarding_step,
    )


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/check-email")
def check_email(email: str, db: Session = Depends(get_db)):
    """Real-time email duplicate check (debounce 500ms on frontend)."""
    exists = db.query(User).filter(User.email == email).first() is not None
    return {"available": not exists}


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


def _send_reset_email(to_email: str, reset_url: str):
    # Prefer SMTP (Gmail) if configured
    if settings.SMTP_HOST and settings.SMTP_USER:
        body = f"""Reset your Kairo password

Click the link below to set a new password (valid for 1 hour):
{reset_url}

If you didn't request this, ignore this email.
— Kairo Team"""
        msg = MIMEText(body)
        msg["Subject"] = "Reset your Kairo password"
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to_email
        try:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD or "")
                smtp.sendmail(settings.SMTP_USER, to_email, msg.as_string())
                logger.info("Reset email sent to %s via SMTP", to_email)
                return  # only return on success
        except Exception as e:
            logger.error("Failed to send reset email via SMTP: %s", e)
            # fall through to Resend

    # Fallback: Resend API
    if not settings.RESEND_API_KEY:
        logger.warning("No email provider configured — reset link: %s", reset_url)
        return
    logger.info("PASSWORD RESET LINK for %s: %s", to_email, reset_url)
    html = f"""<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px">
      <h2>Reset your Kairo password</h2>
      <p>Click the button below to set a new password (valid for 1 hour):</p>
      <a href="{reset_url}" style="display:inline-block;margin:16px 0;padding:12px 24px;background:#00D991;color:#000;font-weight:600;text-decoration:none;border-radius:8px">Reset Password</a>
      <p style="color:#888;font-size:13px">If you didn't request this, ignore this email. — Kairo Team</p>
    </div>"""
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={"from": "Kairo <onboarding@resend.dev>", "to": [to_email], "subject": "Reset your Kairo password", "html": html},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("Reset email sent to %s via Resend", to_email)
        else:
            logger.error("Resend error: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Failed to send reset email via Resend: %s", e)


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    # Always return 200 to avoid user enumeration
    if user and user.hashed_password:
        token = create_reset_token(str(user.id))
        reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        _send_reset_email(user.email, reset_url)
    return {"message": "If that email exists, a reset link has been sent."}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    user_id = decode_reset_token(payload.token)
    if not user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset link")
    if len(payload.password) < 8:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Password must be at least 8 characters")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user.hashed_password = hash_password(payload.password)
    db.commit()
    return {"message": "Password updated successfully"}
