from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Index, UniqueConstraint
from database import Base

class Device(Base):
    """Stores device registration and security tokens."""
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, unique=True, index=True, nullable=False)
    api_token = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

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
    __table_args__ = (
        UniqueConstraint('provider_code', 'billing_month', name='uix_provider_month'),
    )

    id = Column(Integer, primary_key=True, index=True)
    provider_code = Column(String, index=True, nullable=False)
    benchmark_rate = Column(Float, nullable=False)
    billing_month = Column(String, nullable=False)
    status = Column(String, default="SUCCESS")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, unique=True, index=True, nullable=False)
    selected_provider = Column(String, default="MERALCO")
    use_custom_rate = Column(Boolean, default=False)
    custom_rate = Column(Float, nullable=True)
    cycle_start_kwh = Column(Float, default=0.0)  # Stores baseline reading for monthly bill resetting

class TelemetryLog(Base):
    """Time-series logs sent from ESP32 / PZEM-004T."""
    __tablename__ = "telemetry_logs"
    __table_args__ = (
        Index('idx_telemetry_device_time', 'device_id', 'timestamp'),
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True, nullable=False)
    accumulated_kwh = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class ApplianceEvent(Base):
    """Logs classified appliance switching events (TURN_ON / TURN_OFF)."""
    __tablename__ = "appliance_events"
    __table_args__ = (
        Index('idx_events_device_time', 'device_id', 'timestamp'),
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True, nullable=False)
    appliance_name = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    delta_power_w = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class ActiveApplianceSession(Base):
    """Tracks currently active appliances per device for per-appliance cost attribution."""
    __tablename__ = "active_appliance_sessions"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True, nullable=False)
    appliance_name = Column(String, nullable=False)
    power_draw_w = Column(Float, nullable=False)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True)