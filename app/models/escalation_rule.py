import uuid
from sqlalchemy import Column, String, Integer, Numeric, Boolean, DateTime, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class EscalationRule(Base):
    __tablename__ = "escalation_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False)

    trigger = Column(String, nullable=False)  # explicit_request / max_messages / high_value / unknown_question
    threshold = Column(Numeric(10, 2), nullable=True)  # para high_value
    max_messages = Column(Integer, nullable=True)  # para max_messages
    action = Column(String, default="notify_human")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="escalation_rules")
