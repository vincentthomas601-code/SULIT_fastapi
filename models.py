# models.py

from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from database import Base


class ProviderRule(Base):
    """Stores ERC-approved distribution tier matrices and base distribution fees."""
    __tablename__ = "provider_rules"

    id = Column(Integer, primary_key=True, index=True)
    provider_code = Column(String, unique=True, index=True, nullable=False)
    provider_name = Column(String, nullable=False)
    region = Column(String, nullable=False)
    base_distribution_fee = Column(Float, default=0.9803)

    tier_1_max_kwh = Column(Float, default=200.0)
    tier_1_rate = Column(Float, default=0.9803)
    tier_2_max_kwh = Column(Float, default=300.0)
    tier_2_rate = Column(Float, default=1.2908)
    tier_3_max_kwh = Column(Float, default=400.0)
    tier_3_rate = Column(Float, default=1.5837)
    tier_4_rate = Column(Float, default=2.0941)


class ProviderRate(Base):
    """Stores monthly scraped 200 kWh benchmark residential rates (₱/kWh)."""
    __tablename__ = "provider_rates"

    id = Column(Integer, primary_key=True, index=True)
    provider_code = Column(String, index=True, nullable=False)
    benchmark_rate = Column(Float, nullable=False)
    billing_month = Column(String, nullable=False)
    status = Column(String, default="SUCCESS")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, unique=True, index=True, nullable=False)
    selected_provider = Column(String, default="MERALCO")
    use_custom_rate = Column(Boolean, default=False)
    custom_rate = Column(Float, nullable=True)


class TelemetryLog(Base):
    """Time-series logs sent from ESP32 / PZEM-004T."""
    __tablename__ = "telemetry_logs"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True, nullable=False)
    accumulated_kwh = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)


class ApplianceEvent(Base):
    """Logs classified appliance switching events (TURN_ON / TURN_OFF)."""
    __tablename__ = "appliance_events"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True, nullable=False)
    appliance_name = Column(String, nullable=False)
    event_type = Column(String, nullable=False)  # "TURN_ON" or "TURN_OFF"
    delta_power_w = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)


class ActiveApplianceSession(Base):
    """Tracks currently active appliances per device for per-appliance cost attribution."""
    __tablename__ = "active_appliance_sessions"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True, nullable=False)
    appliance_name = Column(String, nullable=False)
    power_draw_w = Column(Float, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)