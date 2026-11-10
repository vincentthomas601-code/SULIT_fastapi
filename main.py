import os
import io
import re
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict
from contextlib import asynccontextmanager

import pdfplumber
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Header, Request, Security, status
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import (
    Device,
    ProviderRule,
    ProviderRate,
    UserSettings,
    TelemetryLog,
    ApplianceEvent,
    ActiveApplianceSession,
)
from billing_engine import calculate_ph_electricity_bill, get_energy_insights
from dsp_features import extract_22d_features
from ml_engine import classifier

logger = logging.getLogger("SULIT_Backend")

# ------------------------------------------------------------------
# Lifespan Lifecycle Manager
# ------------------------------------------------------------------
def seed_default_provider(db: Session):
    existing = db.query(ProviderRule).filter(ProviderRule.provider_code == "MERALCO").first()
    if not existing:
        meralco = ProviderRule(
            provider_code="MERALCO",
            provider_name="Manila Electric Company (MERALCO)",
            region="Luzon Grid (NCR / CALABARZON)",
            base_distribution_fee=0.9803,
            tier_1_max_kwh=200.0,
            tier_1_rate=0.9803,
            tier_2_max_kwh=300.0,
            tier_2_rate=1.2908,
            tier_3_max_kwh=400.0,
            tier_3_rate=1.5837,
            tier_4_rate=2.0941
        )
        db.add(meralco)
        db.commit()
        logger.info("Seeded default MERALCO provider rules.")

def ensure_database_columns():
    if engine.name == "sqlite":
        with engine.connect() as conn:
            user_cols = {col[1] for col in conn.execute(text("PRAGMA table_info(user_settings)")).fetchall()}
            if "device_id" not in user_cols:
                conn.execute(text("ALTER TABLE user_settings ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))
            if "cycle_start_kwh" not in user_cols:
                conn.execute(text("ALTER TABLE user_settings ADD COLUMN cycle_start_kwh FLOAT DEFAULT 0.0"))
            conn.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fix startup ordering: create_all FIRST, then run migrations/seeding
    Base.metadata.create_all(bind=engine)
    ensure_database_columns()
    
    db = next(get_db())
    try:
        seed_default_provider(db)
    finally:
        db.close()
    yield

app = FastAPI(
    title="SULIT PRO Backend",
    version="1.0.0",
    lifespan=lifespan
)

allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True if allowed_origins_env != "*" else False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Device Security Authentication
# ------------------------------------------------------------------
token_header = APIKeyHeader(name="X-Device-Token", auto_error=False)

def verify_device_access(
    x_device_id: str = Header(..., alias="X-Device-ID"),
    x_device_token: Optional[str] = Security(token_header),
    db: Session = Depends(get_db)
) -> str:
    device_id = x_device_id.strip()
    if not re.match(r"^[a-zA-Z0-9_\-]+$", device_id):
        raise HTTPException(status_code=400, detail="Invalid Device ID format.")

    # Bypass auth token check only for explicit default testing identifier
    if device_id == "default_device":
        return device_id

    device_record = db.query(Device).filter(Device.device_id == device_id).first()
    if not device_record:
        if not x_device_token:
            raise HTTPException(status_code=401, detail="X-Device-Token header required for new device registration.")
        device_record = Device(device_id=device_id, api_token=x_device_token)
        db.add(device_record)
        db.commit()
        return device_id

    if device_record.api_token != x_device_token:
        raise HTTPException(status_code=403, detail="Invalid device security token.")

    return device_id

# ------------------------------------------------------------------
# Server-Sent Events Broadcaster (Per-Device Subscription)
# ------------------------------------------------------------------
class EventBroadcaster:
    def __init__(self):
        self.subscribers: Dict[str, list[asyncio.Queue]] = {}

    async def subscribe(self, device_id: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=50)
        if device_id not in self.subscribers:
            self.subscribers[device_id] = []
        self.subscribers[device_id].append(queue)
        return queue

    def unsubscribe(self, device_id: str, queue: asyncio.Queue):
        if device_id in self.subscribers and queue in self.subscribers[device_id]:
            self.subscribers[device_id].remove(queue)

    async def broadcast(self, device_id: str, data: dict):
        if device_id in self.subscribers:
            for queue in list(self.subscribers[device_id]):
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

broadcaster = EventBroadcaster()

# ------------------------------------------------------------------
# Pydantic Schemas with Strict Validations
# ------------------------------------------------------------------
class TelemetryInput(BaseModel):
    device_id: str = Field(..., examples=["ESP32_01"])
    kwh: float = Field(..., ge=0.0, examples=[310.0])

class TransientEventInput(BaseModel):
    device_id: str = Field(..., examples=["ESP32_01"])
    p_before: float = Field(..., ge=0.0)
    p_after: float = Field(..., ge=0.0)
    pf_before: float = Field(..., ge=-1.0, le=1.0)
    pf_after: float = Field(..., ge=-1.0, le=1.0)
    raw_samples: list[float] = Field(..., description="Exactly 6000 SCT-013 current samples (200ms @ 30kHz)")

    @field_validator("raw_samples")
    @classmethod
    def validate_samples(cls, v):
        if len(v) != 6000:
            raise ValueError("raw_samples must contain exactly 6000 numerical float entries.")
        return v

class UserSettingsUpdate(BaseModel):
    selected_provider: Optional[str] = None
    use_custom_rate: Optional[bool] = None
    custom_rate: Optional[float] = Field(None, ge=0.0, le=100.0)
    reset_cycle: Optional[bool] = False

# ------------------------------------------------------------------
# Routes & Controller Logic
# ------------------------------------------------------------------
def get_or_create_device_settings(device_id: str, db: Session) -> UserSettings:
    settings = db.query(UserSettings).filter(UserSettings.device_id == device_id).first()
    if not settings:
        settings = UserSettings(
            device_id=device_id,
            selected_provider="MERALCO",
            use_custom_rate=False,
            cycle_start_kwh=0.0
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings

@app.get("/")
def serve_dashboard():
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI file not found.")
    return FileResponse(index_path)

@app.get("/api/v1/events/stream")
async def stream_events(
    request: Request,
    device_id: str = Depends(verify_device_access)
):
    async def event_generator():
        queue = await broadcaster.subscribe(device_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.unsubscribe(device_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )

@app.post("/api/v1/telemetry")
async def ingest_telemetry(
    data: TelemetryInput, 
    device_id: str = Depends(verify_device_access),
    db: Session = Depends(get_db)
):
    settings = get_or_create_device_settings(device_id, db)

    # Automatically initialize cycle start baseline if counter reset occurs
    if data.kwh < settings.cycle_start_kwh:
        settings.cycle_start_kwh = data.kwh
        db.commit()

    log_entry = TelemetryLog(device_id=device_id, accumulated_kwh=data.kwh)
    db.add(log_entry)
    db.commit()

    await broadcaster.broadcast(device_id, {
        "type": "TELEMETRY_UPDATED",
        "device_id": device_id,
        "accumulated_kwh": data.kwh,
        "net_kwh": max(0.0, data.kwh - settings.cycle_start_kwh)
    })

    return {"status": "SUCCESS", "message": "Telemetry stored successfully."}

@app.post("/api/v1/events/transient")
async def process_transient_event(
    data: TransientEventInput,
    device_id: str = Depends(verify_device_access),
    db: Session = Depends(get_db)
):
    delta_p = data.p_after - data.p_before

    # TURN ON BRANCH: Perform DSP + Classification only on positive transients
    if delta_p >= 15.0:
        features_22d = extract_22d_features(
            raw_samples=data.raw_samples,
            p_before=data.p_before,
            p_after=data.p_after,
            pf_before=data.pf_before,
            pf_after=data.pf_after
        )
        appliance_name, confidence = classifier.predict(features_22d)

        event_log = ApplianceEvent(
            device_id=device_id,
            appliance_name=appliance_name,
            event_type="TURN_ON",
            delta_power_w=delta_p,
            confidence=confidence
        )
        session_entry = ActiveApplianceSession(
            device_id=device_id,
            appliance_name=appliance_name,
            power_draw_w=delta_p,
            is_active=True
        )
        db.add(event_log)
        db.add(session_entry)
        db.commit()

        await broadcaster.broadcast(device_id, {
            "type": "TRANSIENT_DETECTED",
            "device_id": device_id,
            "event_type": "TURN_ON",
            "appliance_name": appliance_name,
            "delta_power_w": round(delta_p, 2),
            "confidence": confidence
        })

        return {"status": "SUCCESS", "event": "TURN_ON", "appliance": appliance_name, "delta_power_w": round(delta_p, 2), "confidence": confidence}

    # TURN OFF BRANCH: Match nearest power step without heavy DSP
    elif delta_p <= -15.0:
        abs_delta = abs(delta_p)
        active_sessions = db.query(ActiveApplianceSession).filter(
            ActiveApplianceSession.device_id == device_id,
            ActiveApplianceSession.is_active.is_(True)
        ).all()

        if not active_sessions:
            return {"status": "SUCCESS", "event": "TURN_OFF", "message": "No matching active session found."}

        closest_session = min(active_sessions, key=lambda s: abs(s.power_draw_w - abs_delta))
        closest_session.is_active = False
        
        event_log = ApplianceEvent(
            device_id=device_id,
            appliance_name=closest_session.appliance_name,
            event_type="TURN_OFF",
            delta_power_w=delta_p,
            confidence=1.0
        )
        db.add(event_log)
        db.commit()

        await broadcaster.broadcast(device_id, {
            "type": "TRANSIENT_DETECTED",
            "device_id": device_id,
            "event_type": "TURN_OFF",
            "appliance_name": closest_session.appliance_name,
            "delta_power_w": round(delta_p, 2)
        })

        return {"status": "SUCCESS", "event": "TURN_OFF", "appliance": closest_session.appliance_name, "delta_power_w": round(delta_p, 2)}

    return {"status": "IGNORED", "message": "Power step below detection threshold."}

@app.get("/api/v1/billing/live")
def get_live_billing(
    device_id: str = Depends(verify_device_access),
    db: Session = Depends(get_db)
):
    settings = get_or_create_device_settings(device_id, db)

    latest_telemetry = (
        db.query(TelemetryLog)
        .filter(TelemetryLog.device_id == device_id)
        .order_by(TelemetryLog.id.desc())
        .first()
    )

    if not latest_telemetry:
        raise HTTPException(status_code=404, detail=f"No telemetry found for device '{device_id}'.")

    # Net monthly cycle kWh calculation
    net_kwh = max(0.0, latest_telemetry.accumulated_kwh - settings.cycle_start_kwh)

    provider_code = settings.selected_provider or "MERALCO"
    rule = db.query(ProviderRule).filter(ProviderRule.provider_code == provider_code).first()
    if not rule:
        rule = db.query(ProviderRule).first()

    if settings.use_custom_rate and settings.custom_rate and settings.custom_rate > 0:
        active_rate = settings.custom_rate
        rate_status = "CUSTOM_OVERRIDE"
        total_bill = round(net_kwh * active_rate, 2)
        dist_charge = round(rule.base_distribution_fee, 2) if (rule and rule.base_distribution_fee) else 0.0
        var_charge = max(0.0, round(total_bill - dist_charge, 2))

        billing_breakdown = {
            "total_bill_php": total_bill,
            "effective_rate_per_kwh": round(active_rate, 4),
            "variable_charge_php": var_charge,
            "distribution_charge_php": dist_charge,
            "pre_discount_subtotal_php": total_bill,
            "lifeline_discount_percentage": 0,
            "lifeline_discount_php": 0.0,
            "energy_tax_php": 0.0
        }
    else:
        latest_rate_entry = (
            db.query(ProviderRate)
            .filter(ProviderRate.provider_code == provider_code)
            .order_by(ProviderRate.id.desc())
            .first()
        )
        active_rate = latest_rate_entry.benchmark_rate if latest_rate_entry else 14.50
        rate_status = latest_rate_entry.status if latest_rate_entry else "FALLBACK"

        billing_breakdown = calculate_ph_electricity_bill(
            net_cycle_kwh=net_kwh,
            benchmark_rate=active_rate,
            provider_rule=rule
        )

    return {
        "accumulated_kwh": round(latest_telemetry.accumulated_kwh, 2),
        "net_cycle_kwh": round(net_kwh, 2),
        "total_bill_php": billing_breakdown.get("total_bill_php", 0.0),
        "effective_rate_per_kwh": billing_breakdown.get("effective_rate_per_kwh", active_rate),
        "variable_charge_php": billing_breakdown.get("variable_charge_php", 0.0),
        "distribution_charge_php": billing_breakdown.get("distribution_charge_php", 0.0),
        "pre_discount_subtotal_php": billing_breakdown.get("pre_discount_subtotal_php", 0.0),
        "lifeline_discount_percentage": billing_breakdown.get("lifeline_discount_percentage", 0),
        "lifeline_discount_php": billing_breakdown.get("lifeline_discount_php", 0.0),
        "energy_tax_php": billing_breakdown.get("energy_tax_php", 0.0),
        "provider_code": provider_code,
        "provider_name": rule.provider_name if rule else "Default Provider",
        "region": rule.region if rule else "Luzon Grid",
        "use_custom_rate": settings.use_custom_rate,
        "rate_status": rate_status
    }

def _parse_pdf_sync(contents: bytes) -> str:
    text_out = ""
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_out += t + "\n"
    return text_out

@app.post("/api/v1/billing/upload-ebill")
async def upload_ebill(
    file: UploadFile = File(...),
    device_id: str = Depends(verify_device_access),
    db: Session = Depends(get_db)
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF e-Bills are supported.")

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds maximum 10MB limit.")

    try:
        extracted_text = await run_in_threadpool(_parse_pdf_sync, contents)
    except Exception as pdf_err:
        raise HTTPException(status_code=400, detail=f"Corrupted PDF file: {pdf_err}")

    # FIXED: Removed the invalid \₱ escape sequence. Using [:₱] or [:\s₱] safely.
    amount_match = re.search(r"(?:Total\s*Amount\s*Due|Amount\s*Due|Total\s*Due)\s*[:\s₱]*([\d,]+\.\d{2})", extracted_text, re.IGNORECASE)
    kwh_match = re.search(r"(?:kWh\s*Used|Total\s*kWh|Energy\s*Consumption)\s*[:\s]*([\d,]+(?:\.\d+)?)", extracted_text, re.IGNORECASE)

    if not amount_match or not kwh_match:
        amount_match = re.search(r"₱\s*([\d,]+\.\d{2})", extracted_text)
        kwh_match = re.search(r"([\d,]+)\s*kWh", extracted_text, re.IGNORECASE)

    if not amount_match or not kwh_match:
        raise HTTPException(status_code=422, detail="Unable to extract kWh or Total Amount from PDF bill.")

    total_amount = float(amount_match.group(1).replace(",", ""))
    total_kwh = float(kwh_match.group(1).replace(",", ""))

    if total_kwh <= 0:
        raise HTTPException(status_code=400, detail="Parsed bill kWh must be greater than zero.")

    effective_rate = round(total_amount / total_kwh, 4)

    settings = get_or_create_device_settings(device_id, db)
    settings.use_custom_rate = True
    settings.custom_rate = effective_rate
    db.commit()

    return {
        "status": "SUCCESS",
        "parsed_amount_php": total_amount,
        "parsed_kwh": total_kwh,
        "calculated_effective_rate_php": effective_rate
    }

@app.post("/api/insights")
async def generate_insights(
    device_id: str = Depends(verify_device_access),
    db: Session = Depends(get_db)
):
    # Construct telemetry summary prompt server-side strictly from DB data
    billing_data = get_live_billing(device_id=device_id, db=db)
    active_loads = (
        db.query(ActiveApplianceSession)
        .filter(ActiveApplianceSession.device_id == device_id, ActiveApplianceSession.is_active.is_(True))
        .all()
    )
    loads_text = ", ".join([f"{l.appliance_name} ({l.power_draw_w}W)" for l in active_loads]) or "None active"

    server_constructed_prompt = (
        f"Device ID: {device_id}\n"
        f"Current Rate: ₱{billing_data['effective_rate_per_kwh']}/kWh ({billing_data['provider_name']})\n"
        f"Net Monthly kWh: {billing_data['net_cycle_kwh']} kWh\n"
        f"Est. Monthly Bill: ₱{billing_data['total_bill_php']}\n"
        f"Active Disaggregated Loads: {loads_text}\n"
    )

    try:
        report = await run_in_threadpool(get_energy_insights, server_constructed_prompt)
        return {"status": "success", "insights": report}
    except ValueError as ve:
        raise HTTPException(status_code=503, detail=str(ve))
    except Exception as err:
        logger.exception("AI Insight failed.")
        raise HTTPException(status_code=502, detail="AI Service temporary failure.")

@app.post("/api/v1/settings")
def update_settings(
    payload: UserSettingsUpdate,
    device_id: str = Depends(verify_device_access),
    db: Session = Depends(get_db)
):
    settings = get_or_create_device_settings(device_id, db)

    if payload.selected_provider is not None:
        settings.selected_provider = payload.selected_provider
    if payload.use_custom_rate is not None:
        settings.use_custom_rate = payload.use_custom_rate
    if payload.custom_rate is not None:
        settings.custom_rate = payload.custom_rate

    if payload.reset_cycle:
        latest = (
            db.query(TelemetryLog)
            .filter(TelemetryLog.device_id == device_id)
            .order_by(TelemetryLog.id.desc())
            .first()
        )
        settings.cycle_start_kwh = latest.accumulated_kwh if latest else 0.0

    db.commit()
    return {"status": "SUCCESS", "message": "Settings updated."}