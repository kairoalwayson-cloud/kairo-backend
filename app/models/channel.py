import uuid
from sqlalchemy import Column, String, Boolean, DateTime, JSON, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class Channel(Base):
    __tablename__ = "channels"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False)

    type = Column(String, nullable=False)  # whatsapp / voice / sms / email
    identifier = Column(String, nullable=True)  # número ou email
    provider = Column(String, nullable=True)  # twilio / meta / sendgrid / vapi
    active = Column(Boolean, default=True)
    config = Column(JSON, nullable=True)  # tokens, webhooks, etc.
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="channels")
