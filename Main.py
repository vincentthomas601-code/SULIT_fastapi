import asyncio
import io
import json
import logging
import re
import time
from typing import Optional
import pdfplumber
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import (
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

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Philippine NILM & Smart Billing Backend",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Real-Time SSE Broadcaster
# ------------------------------------------------------------------
class EventBroadcaster:
    def __init__(self):
        self.subscribers: list[asyncio.Queue] = []

    async def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    async def broadcast(self, data: dict):
        for queue in list(self.subscribers):
            await queue.put(data)


broadcaster = EventBroadcaster()


# ------------------------------------------------------------------
# Pydantic Schemas
# ------------------------------------------------------------------
class TelemetryInput(BaseModel):
    device_id: str = Field(..., example="ESP32_01")
    kwh: float = Field(..., example=310.0)


class TransientEventInput(BaseModel):
    device_id: str = Field(..., example="ESP32_01")
    p_before: float = Field(..., example=50.0)
    p_after: float = Field(..., example=350.0)
    pf_before: float = Field(..., example=0.92)
    pf_after: float = Field(..., example=0.98)
    raw_samples: list[float] = Field(..., description="6000 SCT-013 current samples (200ms @ 30kHz)")


class UserSettingsUpdate(BaseModel):
    selected_provider: Optional[str] = None
    use_custom_rate: Optional[bool] = None
    custom_rate: Optional[float] = None


class InsightRequest(BaseModel):
    device_id: str
    prompt_text: str


def normalize_device_id(device_id: Optional[str]) -> str:
    if device_id is None:
        raise HTTPException(status_code=400, detail="Device ID is required.")
    normalized = device_id.strip()
    
    # FOR TESTING ONLY PLACEHOLDER
    # Temporarily allowing 'default_device' by only checking for empty strings
    if not normalized:
        raise HTTPException(status_code=400, detail="A valid device ID is required.")
        
    return normalized


def ensure_database_columns():
    """Add missing columns for SQLite without crashing PostgreSQL."""
    if engine.name == "sqlite":
        with engine.connect() as conn:
            user_columns = conn.execute(text("PRAGMA table_info(user_settings)")).fetchall()
            existing_user_columns = {column[1] for column in user_columns}
            if "device_id" not in existing_user_columns:
                conn.execute(text("ALTER TABLE user_settings ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))

            telemetry_columns = conn.execute(text("PRAGMA table_info(telemetry_logs)")).fetchall()
            existing_telemetry_columns = {column[1] for column in telemetry_columns}
            if "device_id" not in existing_telemetry_columns:
                conn.execute(text("ALTER TABLE telemetry_logs ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))

            conn.commit()


def get_or_create_device_settings(device_id: str, db: Session) -> UserSettings:
    """Helper function to fetch or create settings tied exclusively to a device_id."""
    normalized = normalize_device_id(device_id)
    settings = db.query(UserSettings).filter(UserSettings.device_id == normalized).first()
    if not settings:
        settings = UserSettings(
            device_id=normalized,
            selected_provider="MERALCO",
            use_custom_rate=False
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@app.on_event("startup")
def startup_event():
    ensure_database_columns()
    Base.metadata.create_all(bind=engine)


@app.get("/")
def serve_dashboard():
    """Serves index.html UI at root URL."""
    return FileResponse("index.html")


@app.get("/api/v1/events/stream")
async def stream_events(request: Request):
    """Server-Sent Events (SSE) stream for real-time dashboard updates."""
    async def event_generator():
        queue = await broadcaster.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                data = await queue.get()
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/api/v1/telemetry")
async def ingest_telemetry(data: TelemetryInput, db: Session = Depends(get_db)):
    device_id = normalize_device_id(data.device_id)
    log_entry = TelemetryLog(device_id=device_id, accumulated_kwh=data.kwh)
    db.add(log_entry)
    db.commit()

    await broadcaster.broadcast({
        "type": "TELEMETRY_UPDATED",
        "device_id": device_id,
        "kwh": data.kwh
    })

    return {"status": "SUCCESS", "message": "Telemetry log stored successfully."}


@app.post("/api/v1/events/transient")
async def process_transient_event(data: TransientEventInput, db: Session = Depends(get_db)):
    device_id = normalize_device_id(data.device_id)
    delta_p = data.p_after - data.p_before

    features_22d = extract_22d_features(
        raw_samples=data.raw_samples,
        p_before=data.p_before,
        p_after=data.p_after,
        pf_before=data.pf_before,
        pf_after=data.pf_after
    )

    if delta_p >= 15.0: 
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

        await broadcaster.broadcast({
            "type": "TRANSIENT_DETECTED",
            "device_id": device_id,
            "event_type": "TURN_ON",
            "appliance_name": appliance_name,
            "delta_power_w": round(delta_p, 2),
            "confidence": confidence
        })

        return {
            "status": "SUCCESS",
            "event": "TURN_ON",
            "appliance": appliance_name,
            "delta_power_w": round(delta_p, 2),
            "confidence": confidence
        }

    elif delta_p <= -15.0:
        abs_delta = abs(delta_p)
        active_sessions = db.query(ActiveApplianceSession).filter(
            ActiveApplianceSession.device_id == device_id,
            ActiveApplianceSession.is_active == True
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

        await broadcaster.broadcast({
            "type": "TRANSIENT_DETECTED",
            "device_id": device_id,
            "event_type": "TURN_OFF",
            "appliance_name": closest_session.appliance_name,
            "delta_power_w": round(delta_p, 2),
            "confidence": 1.0
        })

        return {
            "status": "SUCCESS",
            "event": "TURN_OFF",
            "appliance": closest_session.appliance_name,
            "delta_power_w": round(delta_p, 2)
        }

    return {"status": "IGNORED", "message": "Power step below threshold."}


@app.get("/api/v1/billing/live")
def get_live_billing(
    x_device_id: Optional[str] = Header(default="default_device", alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
    device_id = normalize_device_id(x_device_id)
    settings = get_or_create_device_settings(device_id, db)

    latest_telemetry = (
        db.query(TelemetryLog)
        .filter(TelemetryLog.device_id == device_id)
        .order_by(TelemetryLog.id.desc())
        .first()
    )

    if not latest_telemetry:
        raise HTTPException(
            status_code=404,
            detail=f"No telemetry has been recorded for device '{device_id}' yet."
        )

    accumulated_kwh = latest_telemetry.accumulated_kwh

    provider_code = settings.selected_provider or "MERALCO"
    rule = db.query(ProviderRule).filter(ProviderRule.provider_code == provider_code).first()
    if not rule:
        rule = db.query(ProviderRule).first()

    if settings.use_custom_rate and settings.custom_rate and settings.custom_rate > 0:
        active_rate = settings.custom_rate
        rate_status = "CUSTOM_OVERRIDE"

        total_bill = round(accumulated_kwh * active_rate, 2)
        dist_charge = round(rule.base_distribution_fee, 2) if (rule and rule.base_distribution_fee) else 0.0
        if dist_charge > total_bill:
            dist_charge = 0.0
        var_charge = round(total_bill - dist_charge, 2)

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
            accumulated_kwh=accumulated_kwh,
            benchmark_rate=active_rate,
            provider_rule=rule
        )

    return {
        "accumulated_kwh": round(accumulated_kwh, 2),
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


@app.get("/api/v1/providers")
def get_providers(db: Session = Depends(get_db)):
    providers = db.query(ProviderRule).order_by(ProviderRule.region.asc(), ProviderRule.provider_name.asc()).all()
    return [
        {
            "provider_code": p.provider_code,
            "provider_name": p.provider_name,
            "region": p.region,
            "base_distribution_fee": p.base_distribution_fee
        }
        for p in providers
    ]


@app.post("/api/v1/settings")
def update_settings(
    payload: UserSettingsUpdate,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
    device_id = normalize_device_id(x_device_id)
    settings = get_or_create_device_settings(device_id, db)

    if payload.selected_provider is not None:
        settings.selected_provider = payload.selected_provider
    if payload.use_custom_rate is not None:
        settings.use_custom_rate = payload.use_custom_rate
    if payload.custom_rate is not None:
        settings.custom_rate = payload.custom_rate

    db.commit()
    return {"status": "SUCCESS", "message": f"User settings updated successfully for device {device_id}."}


@app.post("/api/v1/billing/upload-ebill")
async def upload_ebill(
    file: UploadFile = File(...),
    x_device_id: str = Header(..., alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
    device_id = normalize_device_id(x_device_id)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    try:
        contents = await file.read()
        extracted_text = ""
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    extracted_text += text + "\n"

        amount_match = re.search(r"(?:Total\s*Amount\s*Due|Amount\s*Due|Total\s*Due)\s*[:\₱]?\s*([\d,]+\.\d{2})", extracted_text, re.IGNORECASE)
        kwh_match = re.search(r"(?:kWh\s*Used|Total\s*kWh|Energy\s*Consumption)\s*[:\s]?\s*([\d,]+(?:\.\d+)?)", extracted_text, re.IGNORECASE)

        if not amount_match or not kwh_match:
            amount_match = re.search(r"₱\s*([\d,]+\.\d{2})", extracted_text)
            kwh_match = re.search(r"([\d,]+)\s*kWh", extracted_text, re.IGNORECASE)

        if not amount_match or not kwh_match:
            raise HTTPException(status_code=422, detail="Unable to extract kWh or Total Amount from PDF.")

        total_amount = float(amount_match.group(1).replace(",", ""))
        total_kwh = float(kwh_match.group(1).replace(",", ""))

        if total_kwh <= 0:
            raise HTTPException(status_code=400, detail="Parsed kWh must be greater than zero.")

        effective_rate = round(total_amount / total_kwh, 4)

        settings = get_or_create_device_settings(device_id, db)
        settings.use_custom_rate = True
        settings.custom_rate = effective_rate
        db.commit()

        return {
            "status": "SUCCESS",
            "parsed_amount_php": total_amount,
            "parsed_kwh": total_kwh,
            "calculated_effective_rate_php": effective_rate,
            "message": f"Successfully parsed e-Bill. Effective rate set to ₱{effective_rate}/kWh for device {device_id}."
        }

    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Failed to process PDF: {str(err)}")


@app.post("/api/insights")
def generate_insights(payload: InsightRequest, db: Session = Depends(get_db)):
    device_id = normalize_device_id(payload.device_id)
    get_or_create_device_settings(device_id, db)

    max_retries = 3
    base_delay = 2  

    for attempt in range(max_retries):
        try:
            report = get_energy_insights(payload.prompt_text)
            return {"status": "success", "insights": report}
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            is_transient_error = (
                status_code == 429
                or (isinstance(status_code, int) and status_code >= 500)
                or isinstance(e, (TimeoutError, ConnectionError))
            )
            
            if is_transient_error and attempt < max_retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue

            logger.exception("AI insight generation failed")
            if is_transient_error:
                raise HTTPException(
                    status_code=503,
                    detail="Groq AI is temporarily unavailable. Please try again shortly."
                ) from e
            if isinstance(e, RuntimeError) and "GROQ_API_KEY is missing" in str(e):
                raise HTTPException(
                    status_code=503,
                    detail="AI service is not configured. Set the GROQ_API_KEY environment variable."
                ) from e
            raise HTTPException(
                status_code=502,
                detail="Groq AI could not generate insights. Check the server logs for details."
            ) from e


@app.get("/api/v1/appliances/active")
def get_active_appliances(
    x_device_id: str = Header(..., alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
    """Returns currently running appliances for the device session."""
    device_id = normalize_device_id(x_device_id)
    active_sessions = db.query(ActiveApplianceSession).filter(
        ActiveApplianceSession.device_id == device_id,
        ActiveApplianceSession.is_active == True
    ).all()

    return [
        {
            "id": s.id,
            "appliance_name": s.appliance_name,
            "power_draw_w": s.power_draw_w,
            "started_at": s.started_at.isoformat()
        }
        for s in active_sessions
    ]


@app.get("/api/v1/events/recent")
def get_recent_events(
    x_device_id: str = Header(..., alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
    """Returns recent transient switching event logs for audit."""
    device_id = normalize_device_id(x_device_id)
    events = (
        db.query(ApplianceEvent)
        .filter(ApplianceEvent.device_id == device_id)
        .order_by(ApplianceEvent.id.desc())
        .limit(20)
        .all()
    )

    return [
        {
            "id": e.id,
            "appliance_name": e.appliance_name,
            "event_type": e.event_type,
            "delta_power_w": e.delta_power_w,
            "confidence": e.confidence,
            "timestamp": e.timestamp.isoformat()
        }
        for e in events
    ]