import uuid
from sqlalchemy import Column, String, Boolean, DateTime, JSON, Integer, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class Lead(Base):
    __tablename__ = "leads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False)

    # Contact info
    name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    language = Column(String, default="pt")

    # Request info
    service_requested = Column(String, nullable=True)
    location = Column(String, nullable=True)
    location_valid = Column(Boolean, nullable=True)  # dentro ou fora da zona
    notes = Column(String, nullable=True)

    # Status
    # new / qualified / scheduled / follow_up / waiting_human / cold / won / lost
    status = Column(String, default="new")
    channel = Column(String, nullable=True)  # whatsapp / voice / sms / email

    # Scheduling
    appointment_at = Column(DateTime(timezone=True), nullable=True)
    google_event_id = Column(String, nullable=True)
    vapi_call_id = Column(String, nullable=True)

    # Follow-up
    last_message_at = Column(DateTime(timezone=True), nullable=True)
    follow_up_count = Column(Integer, default=0)
    follow_up_scheduled_at = Column(DateTime(timezone=True), nullable=True)

    # Extra
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    business = relationship("Business", back_populates="leads")
    conversations = relationship("Conversation", back_populates="lead", cascade="all, delete-orphan")
