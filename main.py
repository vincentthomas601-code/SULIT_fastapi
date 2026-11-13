import asyncio
from contextlib import asynccontextmanager
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import pdfplumber
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Header, Request, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import APIKeyHeader
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

API_KEY = os.getenv("API_KEY", "change_this_to_a_secure_random_string_in_production")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_database_columns()
    logger.info("Application startup complete.")
    yield
    logger.info("Application shutdown.")


app = FastAPI(
    title="Philippine NILM & Smart Billing Backend",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:8000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EventBroadcaster:
    def __init__(self):
        self.subscribers: dict[str, list[asyncio.Queue]] = {}

    async def subscribe(self, device_id: str) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=100)
        if device_id not in self.subscribers:
            self.subscribers[device_id] = []
        self.subscribers[device_id].append(queue)
        return queue

    def unsubscribe(self, device_id: str, queue: asyncio.Queue):
        if device_id in self.subscribers and queue in self.subscribers[device_id]:
            self.subscribers[device_id].remove(queue)
            if not self.subscribers[device_id]:
                del self.subscribers[device_id]

    async def broadcast(self, data: dict):
        device_id = data.get("device_id")
        if device_id in self.subscribers:
            for queue in list(self.subscribers[device_id]):
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass


broadcaster = EventBroadcaster()


class TelemetryInput(BaseModel):
    device_id: str = Field(..., json_schema_extra={"example": "ESP32_01"})
    kwh: float = Field(..., json_schema_extra={"example": 310.0})


class TransientEventInput(BaseModel):
    device_id: str = Field(..., json_schema_extra={"example": "ESP32_01"})
    p_before: float = Field(..., json_schema_extra={"example": 50.0})
    p_after: float = Field(..., json_schema_extra={"example": 350.0})
    pf_before: float = Field(..., json_schema_extra={"example": 0.92})
    pf_after: float = Field(..., json_schema_extra={"example": 0.98})
    sample_rate_hz: int = Field(30000, description="The device's own sampling rate in Hz")
    raw_samples: list[float] = Field(..., description="6000 SCT-013 current samples (200ms @ 30kHz)")


class UserSettingsUpdate(BaseModel):
    selected_provider: Optional[str] = None
    use_custom_rate: Optional[bool] = None
    custom_rate: Optional[float] = None
    monthly_budget_php: Optional[float] = None


class InsightRequest(BaseModel):
    device_id: str


def normalize_device_id(device_id: Optional[str]) -> str:
    if not device_id or not device_id.strip():
        raise HTTPException(status_code=400, detail="A valid device ID is required.")
    return device_id.strip()


def ensure_database_columns():
    if engine.name == "sqlite":
        with engine.connect() as conn:
            user_columns = {col[1] for col in conn.execute(text("PRAGMA table_info(user_settings)")).fetchall()}
            if user_columns:
                if "device_id" not in user_columns:
                    conn.execute(text("ALTER TABLE user_settings ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))
                if "monthly_budget_php" not in user_columns:
                    conn.execute(text("ALTER TABLE user_settings ADD COLUMN monthly_budget_php FLOAT DEFAULT NULL"))
                if "cycle_start_kwh" not in user_columns:
                    conn.execute(text("ALTER TABLE user_settings ADD COLUMN cycle_start_kwh FLOAT DEFAULT 0.0"))

            telemetry_columns = {col[1] for col in conn.execute(text("PRAGMA table_info(telemetry_logs)")).fetchall()}
            if telemetry_columns:
                if "device_id" not in telemetry_columns:
                    conn.execute(text("ALTER TABLE telemetry_logs ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))
            conn.commit()
    elif engine.name in ["postgresql", "postgres"]:
        with engine.connect() as conn:
            user_query = text("SELECT column_name FROM information_schema.columns WHERE table_name = 'user_settings'")
            user_columns = {row[0] for row in conn.execute(user_query).fetchall()}
            if user_columns:
                if "device_id" not in user_columns:
                    conn.execute(text("ALTER TABLE user_settings ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))
                if "monthly_budget_php" not in user_columns:
                    conn.execute(text("ALTER TABLE user_settings ADD COLUMN monthly_budget_php DOUBLE PRECISION DEFAULT NULL"))
                if "cycle_start_kwh" not in user_columns:
                    conn.execute(text("ALTER TABLE user_settings ADD COLUMN cycle_start_kwh DOUBLE PRECISION DEFAULT 0.0"))

            telemetry_query = text("SELECT column_name FROM information_schema.columns WHERE table_name = 'telemetry_logs'")
            telemetry_columns = {row[0] for row in conn.execute(telemetry_query).fetchall()}
            if telemetry_columns:
                if "device_id" not in telemetry_columns:
                    conn.execute(text("ALTER TABLE telemetry_logs ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))
            conn.commit()


def get_or_create_device_settings(device_id: str, db: Session) -> UserSettings:
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


@app.get("/")
def serve_dashboard():
    return FileResponse("index.html")


@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/v1/events/stream")
async def stream_events(request: Request, device_id: str = Query(...), token: str = Query(...)):
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")

    device_id = normalize_device_id(device_id)

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
                    yield f"data: {json.dumps({'type': 'HEARTBEAT'})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.unsubscribe(device_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.post("/api/v1/telemetry", dependencies=[Depends(verify_api_key)])
def ingest_telemetry(data: TelemetryInput, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    device_id = normalize_device_id(data.device_id)
    log_entry = TelemetryLog(device_id=device_id, accumulated_kwh=data.kwh)
    db.add(log_entry)
    db.commit()

    background_tasks.add_task(
        broadcaster.broadcast,
        {
            "type": "TELEMETRY_UPDATED",
            "device_id": device_id,
            "kwh": data.kwh
        }
    )
    return {"status": "SUCCESS", "message": "Telemetry log stored successfully."}


@app.post("/api/v1/events/transient", dependencies=[Depends(verify_api_key)])
def process_transient_event(data: TransientEventInput, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    device_id = normalize_device_id(data.device_id)
    delta_p = float(data.p_after - data.p_before)

    if delta_p >= 15.0:
        try:
            features_22d = extract_22d_features(
                raw_samples=data.raw_samples, p_before=data.p_before, p_after=data.p_after,
                pf_before=data.pf_before, pf_after=data.pf_after
            )
            raw_name, raw_conf = classifier.predict(features_22d)
            appliance_name = str(raw_name)
            confidence = float(raw_conf)
        except Exception as exc:
            logger.exception("NILM inference failed during TURN_ON")
            raise HTTPException(status_code=500, detail=f"NILM inference error: {type(exc).__name__}: {exc}")

        event_log = ApplianceEvent(
            device_id=device_id, appliance_name=appliance_name,
            event_type="TURN_ON", delta_power_w=float(delta_p), confidence=float(confidence)
        )
        session_entry = ActiveApplianceSession(
            device_id=device_id, appliance_name=appliance_name,
            power_draw_w=float(delta_p), is_active=True
        )
        db.add_all([event_log, session_entry])
        db.commit()

        background_tasks.add_task(
            broadcaster.broadcast,
            {
                "type": "TRANSIENT_DETECTED", "device_id": device_id,
                "event_type": "TURN_ON", "appliance_name": appliance_name,
                "delta_power_w": round(float(delta_p), 2), "confidence": round(float(confidence), 4)
            }
        )
        return {"status": "SUCCESS", "event": "TURN_ON", "appliance": appliance_name, "delta_power_w": round(float(delta_p), 2)}

    elif delta_p <= -15.0:
        abs_delta = abs(delta_p)
        active_sessions = db.query(ActiveApplianceSession).filter(
            ActiveApplianceSession.device_id == device_id, ActiveApplianceSession.is_active == True
        ).all()

        if not active_sessions:
            return {"status": "SUCCESS", "event": "TURN_OFF", "message": "No matching active session found."}

        closest_session = min(active_sessions, key=lambda s: abs(float(s.power_draw_w) - abs_delta))
        closest_session.is_active = False

        event_log = ApplianceEvent(
            device_id=device_id, appliance_name=closest_session.appliance_name,
            event_type="TURN_OFF", delta_power_w=float(delta_p), confidence=1.0
        )
        db.add(event_log)
        db.commit()

        background_tasks.add_task(
            broadcaster.broadcast,
            {
                "type": "TRANSIENT_DETECTED", "device_id": device_id,
                "event_type": "TURN_OFF", "appliance_name": closest_session.appliance_name,
                "delta_power_w": round(float(delta_p), 2), "confidence": None
            }
        )
        return {"status": "SUCCESS", "event": "TURN_OFF", "appliance": closest_session.appliance_name, "delta_power_w": round(float(delta_p), 2)}

    return {"status": "IGNORED", "message": "Power step below threshold."}"delta_power_w": round(delta_p, 2)}

    elif delta_p <= -15.0:
        abs_delta = abs(delta_p)
        active_sessions = db.query(ActiveApplianceSession).filter(
            ActiveApplianceSession.device_id == device_id, ActiveApplianceSession.is_active == True
        ).all()

        if not active_sessions:
            return {"status": "SUCCESS", "event": "TURN_OFF", "message": "No matching active session found."}

        closest_session = min(active_sessions, key=lambda s: abs(float(s.power_draw_w) - abs_delta))
        closest_session.is_active = False

        # FIXED: ApplianceEvent.confidence is NOT NULL in models.py
        event_log = ApplianceEvent(
            device_id=device_id, appliance_name=closest_session.appliance_name,
            event_type="TURN_OFF", delta_power_w=delta_p, confidence=1.0
        )
        db.add(event_log)
        db.commit()

        background_tasks.add_task(
            broadcaster.broadcast,
            {
                "type": "TRANSIENT_DETECTED", "device_id": device_id,
                "event_type": "TURN_OFF", "appliance_name": closest_session.appliance_name,
                "delta_power_w": round(delta_p, 2), "confidence": None
            }
        )
        return {"status": "SUCCESS", "event": "TURN_OFF", "appliance": closest_session.appliance_name, "delta_power_w": round(delta_p, 2)}

    return {"status": "IGNORED", "message": "Power step below threshold."}


@app.get("/api/v1/billing/live", dependencies=[Depends(verify_api_key)])
def get_live_billing(x_device_id: Optional[str] = Header(default="default_device", alias="X-Device-ID"), db: Session = Depends(get_db)):
    device_id = normalize_device_id(x_device_id)
    settings = get_or_create_device_settings(device_id, db)

    latest_telemetry = (
        db.query(TelemetryLog)
        .filter(TelemetryLog.device_id == device_id)
        .order_by(TelemetryLog.id.desc())
        .first()
    )

    if not latest_telemetry:
        return {
            "has_data": False,
            "current_bill_php": 0.0,
            "consumed_kwh": 0.0,
            "message": "No telemetry data found for this device."
        }

    cycle_start = float(settings.cycle_start_kwh) if settings.cycle_start_kwh else 0.0
    net_accumulated_kwh = max(0.0, latest_telemetry.accumulated_kwh - cycle_start)

    provider_code = settings.selected_provider
    rule = db.query(ProviderRule).filter(ProviderRule.provider_code == provider_code).first() if provider_code else None

    if settings.use_custom_rate and settings.custom_rate and float(settings.custom_rate) > 0:
        active_rate = float(settings.custom_rate)
        rate_status = "CUSTOM_OVERRIDE"
        total_bill = round(float(net_accumulated_kwh) * active_rate, 2)

        billing_breakdown = {
            "total_bill_php": total_bill,
            "effective_rate_per_kwh": round(active_rate, 4),
            "variable_charge_php": total_bill,
            "distribution_charge_php": 0.0
        }
    else:
        latest_rate_entry = None
        if provider_code:
            latest_rate_entry = (
                db.query(ProviderRate)
                .filter(ProviderRate.provider_code == provider_code)
                .order_by(ProviderRate.billing_month.desc(), ProviderRate.id.desc())
                .first()
            )

        active_rate = float(latest_rate_entry.benchmark_rate) if latest_rate_entry else 15.0615
        rate_status = latest_rate_entry.status if latest_rate_entry else "DEFAULT_FALLBACK"

        if rule and active_rate > 0:
            billing_breakdown = calculate_ph_electricity_bill(
                net_cycle_kwh=net_accumulated_kwh,
                benchmark_rate=active_rate,
                provider_rule=rule
            )
        else:
            billing_breakdown = {}

    budget_metrics = {}
    if settings.monthly_budget_php and settings.monthly_budget_php > 0:
        budget = float(settings.monthly_budget_php)
        current_bill = billing_breakdown.get("total_bill_php", 0.0)
        remaining = max(0.0, budget - current_bill)
        budget_metrics = {
            "monthly_budget_php": budget,
            "budget_remaining_php": round(remaining, 2),
            "budget_used_percentage": round((current_bill / budget) * 100, 2),
            "is_over_budget": current_bill > budget
        }

    return {
        "has_data": True,
        "raw_device_kwh": round(float(latest_telemetry.accumulated_kwh), 2),
        "cycle_start_kwh": cycle_start,
        "net_accumulated_kwh": round(net_accumulated_kwh, 2),
        "total_bill_php": billing_breakdown.get("total_bill_php", 0.0),
        "effective_rate_per_kwh": billing_breakdown.get("effective_rate_per_kwh", active_rate),
        "variable_charge_php": billing_breakdown.get("variable_charge_php", 0.0),
        "distribution_charge_php": billing_breakdown.get("distribution_charge_php", 0.0),
        "lifeline_discount_php": billing_breakdown.get("lifeline_discount_php", 0.0),
        "energy_tax_php": billing_breakdown.get("energy_tax_php", 0.0),
        "provider_code": provider_code or "NONE",
        "provider_name": rule.provider_name if rule else "No Provider Selected",
        "region": rule.region if rule else "Unknown",
        "use_custom_rate": settings.use_custom_rate,
        "rate_status": rate_status,
        **budget_metrics
    }


@app.post("/api/v1/billing/reset", dependencies=[Depends(verify_api_key)])
def reset_billing_cycle(x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    device_id = normalize_device_id(x_device_id)
    settings = get_or_create_device_settings(device_id, db)

    latest_telemetry = db.query(TelemetryLog).filter(TelemetryLog.device_id == device_id).order_by(TelemetryLog.id.desc()).first()
    if not latest_telemetry:
        raise HTTPException(status_code=404, detail="No telemetry available to reset from.")

    settings.cycle_start_kwh = latest_telemetry.accumulated_kwh
    db.commit()

    return {
        "status": "SUCCESS",
        "message": "Billing cycle reset successfully.",
        "new_cycle_start_kwh": latest_telemetry.accumulated_kwh
    }


@app.get("/api/v1/providers", dependencies=[Depends(verify_api_key)])
def get_providers(db: Session = Depends(get_db)):
    providers = db.query(ProviderRule).order_by(ProviderRule.region.asc(), ProviderRule.provider_name.asc()).all()
    return [
        {
            "provider_code": p.provider_code,
            "provider_name": p.provider_name,
            "region": p.region,
            "base_distribution_fee": float(p.base_distribution_fee) if p.base_distribution_fee else 0.0
        }
        for p in providers
    ]


@app.post("/api/v1/providers/sync", dependencies=[Depends(verify_api_key)])
def trigger_rate_sync():
    from scraper import scrape_utility_rates
    result = scrape_utility_rates(exit_on_error=False)

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=f"Sync failed: {result.get('message', 'Unknown error')}")
    return result


@app.post("/api/v1/settings", dependencies=[Depends(verify_api_key)])
def update_settings(payload: UserSettingsUpdate, x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    device_id = normalize_device_id(x_device_id)
    settings = get_or_create_device_settings(device_id, db)

    if payload.selected_provider is not None:
        settings.selected_provider = payload.selected_provider
    if payload.use_custom_rate is not None:
        settings.use_custom_rate = payload.use_custom_rate
    if payload.custom_rate is not None:
        settings.custom_rate = payload.custom_rate
    if payload.monthly_budget_php is not None:
        settings.monthly_budget_php = payload.monthly_budget_php

    db.commit()
    return {"status": "SUCCESS", "message": f"User settings updated successfully for device {device_id}."}


@app.post("/api/v1/billing/upload-ebill", dependencies=[Depends(verify_api_key)])
def upload_ebill(file: UploadFile = File(...), x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    device_id = normalize_device_id(x_device_id)
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    try:
        contents = file.file.read()
        extracted_text = ""
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            for page in pdf.pages:
                text_content = page.extract_text()
                if text_content:
                    extracted_text += text_content + "\n"

        amount_match = re.search(r"(?:Total\s*Amount\s*Due|Amount\s*to\s*Pay|Current\s*Charges)\s*[:₱]?\s*([\d,]+\.\d{2})", extracted_text, re.IGNORECASE)
        kwh_match = re.search(r"(?:Total\s*kWh|Actual\s*Consumption|Consumption)\s*[:\s]?\s*([\d,]+(?:\.\d+)?)[\s]*kWh", extracted_text, re.IGNORECASE)

        if not amount_match or not kwh_match:
            raise HTTPException(status_code=422, detail="Unable to safely extract kWh or Total Amount Due from PDF.")

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
            "message": f"Successfully parsed e-Bill. Effective rate set to ₱{effective_rate}/kWh."
        }
    except HTTPException:
        raise
    except Exception as err:
        logger.error(f"Failed to process PDF: {str(err)}")
        raise HTTPException(status_code=500, detail=f"Failed to process PDF: {str(err)}")


@app.post("/api/insights", dependencies=[Depends(verify_api_key)])
def generate_insights(payload: InsightRequest, db: Session = Depends(get_db)):
    device_id = normalize_device_id(payload.device_id)
    settings = get_or_create_device_settings(device_id, db)

    latest_telemetry = db.query(TelemetryLog).filter(TelemetryLog.device_id == device_id).order_by(TelemetryLog.id.desc()).first()
    cycle_start = float(settings.cycle_start_kwh) if settings.cycle_start_kwh else 0.0
    current_kwh = float(latest_telemetry.accumulated_kwh) if latest_telemetry else 0.0
    net_accumulated_kwh = max(0.0, current_kwh - cycle_start)

    safe_prompt = f"The user for device '{device_id}' has consumed {net_accumulated_kwh} kWh this billing cycle. Provide brief, actionable energy-saving tips specifically tailored for residential electricity usage."

    max_retries = 3
    base_delay = 2

    for attempt in range(max_retries):
        try:
            report = get_energy_insights(safe_prompt)
            return {"status": "success", "insights": report}
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            is_transient_error = (status_code == 429 or (isinstance(status_code, int) and status_code >= 500) or isinstance(e, (TimeoutError, ConnectionError)))

            if is_transient_error and attempt < max_retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue

            logger.exception("AI insight generation failed")
            if isinstance(e, RuntimeError) and "GROQ_API_KEY is missing" in str(e):
                raise HTTPException(status_code=503, detail="AI service is not configured.") from e
            if is_transient_error:
                raise HTTPException(status_code=503, detail="Groq AI is temporarily unavailable.") from e
            raise HTTPException(status_code=502, detail="Groq AI could not generate insights.") from e


@app.get("/api/v1/appliances/active", dependencies=[Depends(verify_api_key)])
def get_active_appliances(x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    device_id = normalize_device_id(x_device_id)
    active_sessions = db.query(ActiveApplianceSession).filter(ActiveApplianceSession.device_id == device_id, ActiveApplianceSession.is_active == True).all()

    return [
        {
            "id": s.id,
            "appliance_name": s.appliance_name,
            "power_draw_w": float(s.power_draw_w) if s.power_draw_w else 0.0,
            "started_at": s.started_at.replace(tzinfo=timezone.utc).isoformat() if getattr(s, "started_at", None) else None
        }
        for s in active_sessions
    ]


@app.get("/api/v1/events/recent", dependencies=[Depends(verify_api_key)])
def get_recent_events(x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    device_id = normalize_device_id(x_device_id)
    events = db.query(ApplianceEvent).filter(ApplianceEvent.device_id == device_id).order_by(ApplianceEvent.id.desc()).limit(20).all()

    return [
        {
            "id": e.id,
            "appliance_name": e.appliance_name,
            "event_type": e.event_type,
            "delta_power_w": float(e.delta_power_w) if e.delta_power_w else 0.0,
            "confidence": float(e.confidence) if e.confidence else 0.0,
            "timestamp": e.timestamp.replace(tzinfo=timezone.utc).isoformat() if getattr(e, "timestamp", None) else None
        }
        for e in events
    ]