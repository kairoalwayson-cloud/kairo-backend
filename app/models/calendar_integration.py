import uuid
from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class CalendarIntegration(Base):
    __tablename__ = "calendar_integrations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider = Column(String(20), nullable=False)  # "google" | "apple"
    access_token = Column(Text, nullable=True)    # encrypted
    refresh_token = Column(Text, nullable=True)   # encrypted
    token_expiry = Column(DateTime(timezone=True), nullable=True)
    caldav_url = Column(String, nullable=True)
    caldav_username = Column(String, nullable=True)
    caldav_password = Column(Text, nullable=True)  # encrypted
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="calendar_integrations")
