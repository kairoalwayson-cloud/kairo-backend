import uuid
from sqlalchemy import Column, String, Integer, Numeric, DateTime, func, ForeignKey, ARRAY
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class ServiceZone(Base):
    __tablename__ = "service_zones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False)

    type = Column(String, nullable=False)  # radius / neighborhoods / zipcodes
    radius_km = Column(Integer, nullable=True)
    center_lat = Column(Numeric(10, 6), nullable=True)
    center_lng = Column(Numeric(10, 6), nullable=True)
    locations = Column(ARRAY(String), default=[])  # bairros, cidades ou CEPs
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="service_zones")
