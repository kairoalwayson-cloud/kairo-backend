import uuid
from sqlalchemy import Column, String, Boolean, DateTime, JSON, Integer, func, ForeignKey, ARRAY
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class Business(Base):
    __tablename__ = "businesses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # Profile
    name = Column(String, nullable=False)
    owner_name = Column(String, nullable=False)
    logo_url = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    city = Column(String, nullable=True)
    country = Column(String, default="BR")  # BR / US / CL
    website = Column(String, nullable=True)
    categories = Column(ARRAY(String), default=[])

    # AI Config
    ai_name = Column(String, default="Kairo")
    ai_gender = Column(String, default="feminino")  # feminino / masculino / neutro
    ai_tone = Column(String, default="semi-formal")  # formal / semi-formal / casual
    ai_language = Column(String, default="pt")  # pt / en / es
    ai_detect_language = Column(Boolean, default=True)
    welcome_message = Column(String, nullable=True)

    # Schedule
    working_hours = Column(JSON, nullable=True)  # {mon: {start: "08:00", end: "18:00", active: true}, ...}
    timezone = Column(String, default="America/Sao_Paulo")
    emergency_attendance = Column(Boolean, default=False)

    # Booking
    booking_url = Column(String, nullable=True)  # Calendly / Google Appointment Page / etc.

    # Vapi
    vapi_assistant_id = Column(String, nullable=True)

    # Notifications
    notify_on_new_lead = Column(Boolean, default=True)
    notify_on_escalate = Column(Boolean, default=True)
    notify_phone = Column(String, nullable=True)
    quiet_hours_start = Column(Integer, nullable=True)  # hour 0-23
    quiet_hours_end = Column(Integer, nullable=True)

    # Plan / Billing
    plan = Column(String, default="trial")  # trial / starter / pro / scale
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    owner = relationship("User", back_populates="businesses")
    services = relationship("Service", back_populates="business", cascade="all, delete-orphan")
    service_zones = relationship("ServiceZone", back_populates="business", cascade="all, delete-orphan")
    channels = relationship("Channel", back_populates="business", cascade="all, delete-orphan")
    escalation_rules = relationship("EscalationRule", back_populates="business", cascade="all, delete-orphan")
    leads = relationship("Lead", back_populates="business", cascade="all, delete-orphan")
