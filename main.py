import asyncio
from contextlib import asynccontextmanager
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pdfplumber
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Header, Request, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine, get_db
from models import (
    ProviderRule,
    ProviderRate,
    UserSettings,
    TelemetryLog,
    ApplianceEvent,
    ActiveApplianceSession,
    CustomApplianceSignature,
)
from billing_engine import calculate_ph_electricity_bill, get_energy_insights
from dsp_features import extract_features
from ml_engine import classifier

logger = logging.getLogger(__name__)

PH_TZ = timezone(timedelta(hours=8))
API_KEY = os.getenv("API_KEY", "change_this_to_a_secure_random_string_in_production")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key


def _seed_providers_if_empty():
    db = SessionLocal()
    try:
        if db.query(ProviderRule).count() == 0:
            logger.info("provider_rules is empty — running initial PRESYO-PH sync...")
            from scraper import scrape_utility_rates
            scrape_utility_rates(exit_on_error=False)
    except Exception as exc:
        logger.warning(f"Startup provider sync skipped: {exc}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_database_columns()
    threading.Thread(target=_seed_providers_if_empty, daemon=True).start()
    logger.info("Application startup complete.")
    yield
    logger.info("Application shutdown.")


app = FastAPI(
    title="Philippine NILM & Smart Billing Backend",
    version="1.4.0",
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


class RelabelEventInput(BaseModel):
    appliance_name: str = Field(..., min_length=1, max_length=64)
    save_signature: bool = True


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


def _to_utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_crest_factor(raw_samples: list[float]) -> float:
    if not raw_samples:
        return 1.414
    samples = np.nan_to_num(np.asarray(raw_samples, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if samples.size < 4:
        return 1.414
    samples = samples - np.mean(samples)
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms < 1e-6:
        return 1.414
    peak = float(np.max(np.abs(samples)))
    return round(peak / rms, 4)


def _match_custom_signature(
    device_id: str,
    delta_p: float,
    pf_after: float,
    crest_factor: float,
    db: Session,
) -> Optional[tuple[str, float]]:
    signatures = (
        db.query(CustomApplianceSignature)
        .filter(CustomApplianceSignature.device_id == device_id)
        .all()
    )
    if not signatures:
        return None

    best_name: Optional[str] = None
    best_dist = float("inf")

    for sig in signatures:
        sig_w = float(sig.avg_watts or 0.0)
        if sig_w <= 0:
            continue

        tol_w = max(25.0, sig_w * 0.15)
        diff_w = abs(delta_p - sig_w)
        if diff_w > tol_w:
            continue

        diff_pf = 0.0
        if pf_after > 0 and sig.avg_pf and float(sig.avg_pf) > 0:
            diff_pf = abs(pf_after - float(sig.avg_pf))
            if diff_pf > 0.10:
                continue

        diff_cf = 0.0
        if crest_factor > 0 and sig.avg_crest_factor and float(sig.avg_crest_factor) > 0:
            diff_cf = abs(crest_factor - float(sig.avg_crest_factor))
            if diff_cf > 0.45:
                continue

        dist = (diff_w / tol_w) * 0.70 + (diff_pf / 0.10) * 0.15 + (diff_cf / 0.45) * 0.15
        if dist < best_dist:
            best_dist = dist
            best_name = sig.appliance_name

    if best_name is None:
        return None

    confidence = round(max(0.84, min(0.99, 0.99 - best_dist * 0.14)), 4)
    return best_name, confidence


def ensure_database_columns():
    alter_statements = [
        "ALTER TABLE user_settings ADD COLUMN device_id VARCHAR DEFAULT 'default_device'",
        "ALTER TABLE user_settings ADD COLUMN monthly_budget_php FLOAT DEFAULT NULL",
        "ALTER TABLE user_settings ADD COLUMN cycle_start_kwh FLOAT DEFAULT 0.0",
        "ALTER TABLE telemetry_logs ADD COLUMN device_id VARCHAR DEFAULT 'default_device'",
        "ALTER TABLE appliance_events ADD COLUMN pf_after FLOAT DEFAULT NULL",
        "ALTER TABLE appliance_events ADD COLUMN crest_factor FLOAT DEFAULT NULL",
    ]
    for stmt in alter_statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            pass


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


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()
    escaped_key = API_KEY.replace("\\", "\\\\").replace('"', '\\"')
    return HTMLResponse(content=html.replace("__INJECTED_API_KEY__", escaped_key))


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "models_loaded": classifier.get_status()["loaded_count"],
    }


@app.get("/api/v1/ml/status", dependencies=[Depends(verify_api_key)])
def get_ml_status():
    return classifier.get_status()


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
        crest_factor = compute_crest_factor(data.raw_samples)
        matched_custom = _match_custom_signature(
            device_id=device_id,
            delta_p=delta_p,
            pf_after=float(data.pf_after),
            crest_factor=crest_factor,
            db=db,
        )

        if matched_custom is not None:
            appliance_name, confidence = matched_custom
        else:
            try:
                features = extract_features(
                    raw_samples=data.raw_samples,
                    p_before=data.p_before,
                    p_after=data.p_after,
                    pf_before=data.pf_before,
                    pf_after=data.pf_after,
                    target_dim=classifier.expected_features
                )
                raw_name, raw_conf = classifier.predict(
                    features=features,
                    raw_samples=data.raw_samples,
                    delta_p=delta_p,
                )
                appliance_name = str(raw_name)
                confidence = float(raw_conf)
            except Exception as exc:
                logger.exception("NILM inference failed during TURN_ON")
                raise HTTPException(status_code=500, detail=f"NILM inference error: {type(exc).__name__}: {exc}")

        try:
            event_log = ApplianceEvent(
                device_id=device_id,
                appliance_name=appliance_name,
                event_type="TURN_ON",
                delta_power_w=float(delta_p),
                confidence=float(confidence),
                pf_after=float(data.pf_after),
                crest_factor=float(crest_factor),
            )
            session_entry = ActiveApplianceSession(
                device_id=device_id,
                appliance_name=appliance_name,
                power_draw_w=float(delta_p),
                is_active=True,
            )
            db.add_all([event_log, session_entry])
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("Database commit failed during TURN_ON")
            raise HTTPException(status_code=500, detail=f"Database error: {type(exc).__name__}: {exc}")

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
        matched_name = closest_session.appliance_name

        if data.p_after <= 15.0:
            for session in active_sessions:
                session.is_active = False
        elif abs_delta < float(closest_session.power_draw_w) * 0.65:
            # Multi-stage switch step-down (e.g., Hairdryer High -> Low -> Off)
            remaining_w = max(0.0, float(closest_session.power_draw_w) - abs_delta)
            if remaining_w <= 20.0:
                closest_session.is_active = False
            else:
                closest_session.power_draw_w = remaining_w
        else:
            closest_session.is_active = False

        event_log = ApplianceEvent(
            device_id=device_id,
            appliance_name=matched_name,
            event_type="TURN_OFF",
            delta_power_w=float(delta_p),
            confidence=1.0,
            pf_after=float(data.pf_after),
        )
        db.add(event_log)
        db.commit()

        background_tasks.add_task(
            broadcaster.broadcast,
            {
                "type": "TRANSIENT_DETECTED", "device_id": device_id,
                "event_type": "TURN_OFF", "appliance_name": matched_name,
                "delta_power_w": round(float(delta_p), 2), "confidence": None
            }
        )
        return {"status": "SUCCESS", "event": "TURN_OFF", "appliance": matched_name, "delta_power_w": round(float(delta_p), 2)}

    return {"status": "IGNORED", "message": "Power step below threshold."}


@app.post("/api/v1/events/{event_id}/relabel", dependencies=[Depends(verify_api_key)])
def relabel_appliance_event(
    event_id: int,
    payload: RelabelEventInput,
    background_tasks: BackgroundTasks,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    db: Session = Depends(get_db),
):
    device_id = normalize_device_id(x_device_id)
    new_name = payload.appliance_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Appliance name cannot be empty.")

    event = (
        db.query(ApplianceEvent)
        .filter(ApplianceEvent.id == event_id, ApplianceEvent.device_id == device_id)
        .first()
    )
    if not event:
        raise HTTPException(status_code=404, detail="Appliance event not found.")

    old_name = event.appliance_name
    watts = abs(float(event.delta_power_w or 0.0))
    pf_val = float(event.pf_after) if event.pf_after is not None else 0.95
    cf_val = float(event.crest_factor) if event.crest_factor is not None else 1.414

    event.appliance_name = new_name
    event.confidence = 1.0

    # Relabel the matching TURN_OFF event immediately following this TURN_ON (if any)
    if event.event_type == "TURN_ON":
        paired_off = (
            db.query(ApplianceEvent)
            .filter(
                ApplianceEvent.device_id == device_id,
                ApplianceEvent.id > event.id,
                ApplianceEvent.event_type == "TURN_OFF",
                ApplianceEvent.appliance_name == old_name,
            )
            .order_by(ApplianceEvent.id.asc())
            .first()
        )
        if paired_off:
            paired_off.appliance_name = new_name

        # Also update currently running session if it matches the old label and wattage
        active_match = (
            db.query(ActiveApplianceSession)
            .filter(
                ActiveApplianceSession.device_id == device_id,
                ActiveApplianceSession.is_active == True,
                ActiveApplianceSession.appliance_name == old_name,
            )
            .order_by(ActiveApplianceSession.id.desc())
            .first()
        )
        if active_match:
            active_match.appliance_name = new_name

    # Save or update CustomApplianceSignature for future automatic detection
    if payload.save_signature and watts >= 15.0:
        existing_sigs = (
            db.query(CustomApplianceSignature)
            .filter(
                CustomApplianceSignature.device_id == device_id,
                CustomApplianceSignature.appliance_name == new_name,
            )
            .all()
        )

        # Match existing mode within ±20% wattage (keeps Low vs High heat settings distinct)
        same_mode_sig = next(
            (s for s in existing_sigs if abs(float(s.avg_watts) - watts) <= max(25.0, float(s.avg_watts) * 0.20)),
            None,
        )

        if same_mode_sig:
            n = int(same_mode_sig.sample_count or 1)
            same_mode_sig.avg_watts = round((float(same_mode_sig.avg_watts) * n + watts) / (n + 1), 2)
            same_mode_sig.avg_pf = round((float(same_mode_sig.avg_pf or pf_val) * n + pf_val) / (n + 1), 4)
            same_mode_sig.avg_crest_factor = round((float(same_mode_sig.avg_crest_factor or cf_val) * n + cf_val) / (n + 1), 4)
            same_mode_sig.sample_count = n + 1
        else:
            db.add(
                CustomApplianceSignature(
                    device_id=device_id,
                    appliance_name=new_name,
                    avg_watts=round(watts, 2),
                    avg_pf=round(pf_val, 4),
                    avg_crest_factor=round(cf_val, 4),
                    sample_count=1,
                )
            )

    db.commit()

    background_tasks.add_task(
        broadcaster.broadcast,
        {
            "type": "TRANSIENT_DETECTED",
            "device_id": device_id,
            "event_type": "RELABEL",
            "appliance_name": new_name,
        }
    )

    return {
        "status": "SUCCESS",
        "message": f"Updated label to '{new_name}' and saved signature ({round(watts, 1)} W).",
        "appliance_name": new_name,
    }


@app.get("/api/v1/signatures", dependencies=[Depends(verify_api_key)])
def get_custom_signatures(x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    device_id = normalize_device_id(x_device_id)
    sigs = (
        db.query(CustomApplianceSignature)
        .filter(CustomApplianceSignature.device_id == device_id)
        .order_by(CustomApplianceSignature.appliance_name.asc(), CustomApplianceSignature.avg_watts.asc())
        .all()
    )
    return [
        {
            "id": s.id,
            "appliance_name": s.appliance_name,
            "avg_watts": round(float(s.avg_watts), 1),
            "avg_pf": round(float(s.avg_pf or 0.0), 2),
            "sample_count": int(s.sample_count or 1),
        }
        for s in sigs
    ]


@app.delete("/api/v1/signatures/{signature_id}", dependencies=[Depends(verify_api_key)])
def delete_custom_signature(
    signature_id: int,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    db: Session = Depends(get_db),
):
    device_id = normalize_device_id(x_device_id)
    sig = (
        db.query(CustomApplianceSignature)
        .filter(CustomApplianceSignature.id == signature_id, CustomApplianceSignature.device_id == device_id)
        .first()
    )
    if not sig:
        raise HTTPException(status_code=404, detail="Signature not found.")
    db.delete(sig)
    db.commit()
    return {"status": "SUCCESS"}


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

    provider_code = settings.selected_provider
    rule = db.query(ProviderRule).filter(ProviderRule.provider_code == provider_code).first() if provider_code else None

    if settings.use_custom_rate and settings.custom_rate and float(settings.custom_rate) > 0:
        active_rate = float(settings.custom_rate)
        rate_status = "CUSTOM_OVERRIDE"
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

    if not latest_telemetry:
        return {
            "has_data": False,
            "raw_device_kwh": 0.0,
            "cycle_start_kwh": float(settings.cycle_start_kwh or 0.0),
            "net_accumulated_kwh": 0.0,
            "total_bill_php": 0.0,
            "effective_rate_per_kwh": round(active_rate, 4),
            "variable_charge_php": 0.0,
            "distribution_charge_php": 0.0,
            "pre_discount_subtotal_php": 0.0,
            "lifeline_discount_percentage": 0.0,
            "lifeline_discount_php": 0.0,
            "energy_tax_php": 0.0,
            "provider_code": provider_code or "NONE",
            "provider_name": rule.provider_name if rule else "No Provider Selected",
            "region": rule.region if rule else "Unknown",
            "use_custom_rate": settings.use_custom_rate,
            "rate_status": rate_status,
            "message": "No telemetry data found for this device."
        }

    cycle_start = float(settings.cycle_start_kwh) if settings.cycle_start_kwh else 0.0
    net_accumulated_kwh = max(0.0, latest_telemetry.accumulated_kwh - cycle_start)

    if settings.use_custom_rate and settings.custom_rate and float(settings.custom_rate) > 0:
        total_bill = round(float(net_accumulated_kwh) * active_rate, 2)
        billing_breakdown = {
            "total_bill_php": total_bill,
            "effective_rate_per_kwh": round(active_rate, 4),
            "variable_charge_php": total_bill,
            "distribution_charge_php": 0.0
        }
    else:
        if rule and active_rate > 0:
            billing_breakdown = calculate_ph_electricity_bill(
                net_cycle_kwh=net_accumulated_kwh,
                benchmark_rate=active_rate,
                provider_rule=rule
            )
        else:
            billing_breakdown = {}

    display_rate = billing_breakdown.get("effective_rate_per_kwh", active_rate)
    if (display_rate is None or float(display_rate) == 0.0) and active_rate > 0:
        display_rate = round(active_rate, 4)

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
        "effective_rate_per_kwh": display_rate,
        "variable_charge_php": billing_breakdown.get("variable_charge_php", 0.0),
        "distribution_charge_php": billing_breakdown.get("distribution_charge_php", 0.0),
        "pre_discount_subtotal_php": billing_breakdown.get("pre_discount_subtotal_php", 0.0),
        "lifeline_discount_percentage": billing_breakdown.get("lifeline_discount_percentage", 0.0),
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
def reset_billing_cycle(
    background_tasks: BackgroundTasks,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    db: Session = Depends(get_db),
):
    device_id = normalize_device_id(x_device_id)
    settings = get_or_create_device_settings(device_id, db)

    latest_telemetry = (
        db.query(TelemetryLog)
        .filter(TelemetryLog.device_id == device_id)
        .order_by(TelemetryLog.id.desc())
        .first()
    )

    baseline_kwh = float(latest_telemetry.accumulated_kwh) if latest_telemetry else 0.0
    settings.cycle_start_kwh = baseline_kwh

    db.query(TelemetryLog).filter(TelemetryLog.device_id == device_id).delete(synchronize_session=False)
    if latest_telemetry:
        db.add(TelemetryLog(device_id=device_id, accumulated_kwh=baseline_kwh))

    db.query(ActiveApplianceSession).filter(
        ActiveApplianceSession.device_id == device_id,
        ActiveApplianceSession.is_active == True,
    ).update({"is_active": False}, synchronize_session=False)
    db.query(ApplianceEvent).filter(ApplianceEvent.device_id == device_id).delete(synchronize_session=False)

    db.commit()

    background_tasks.add_task(
        broadcaster.broadcast,
        {
            "type": "CYCLE_RESET",
            "device_id": device_id,
            "new_cycle_start_kwh": baseline_kwh,
        }
    )

    return {
        "status": "SUCCESS",
        "message": "Billing cycle, active loads, and usage charts reset successfully.",
        "new_cycle_start_kwh": baseline_kwh
    }


@app.get("/api/v1/analytics/charts", dependencies=[Depends(verify_api_key)])
def get_chart_analytics(
    range_mode: str = Query(default="24h", alias="range"),
    x_device_id: Optional[str] = Header(default="default_device", alias="X-Device-ID"),
    db: Session = Depends(get_db),
):
    device_id = normalize_device_id(x_device_id)
    now_utc = datetime.now(timezone.utc)
    now_ph = now_utc.astimezone(PH_TZ)

    events = (
        db.query(ApplianceEvent)
        .filter(ApplianceEvent.device_id == device_id)
        .order_by(ApplianceEvent.timestamp.asc(), ApplianceEvent.id.asc())
        .all()
    )
    active_sessions = (
        db.query(ActiveApplianceSession)
        .filter(ActiveApplianceSession.device_id == device_id, ActiveApplianceSession.is_active == True)
        .all()
    )

    appliance_kwh: dict[str, float] = {}
    open_events: dict[str, list[tuple[datetime, float]]] = {}

    for ev in events:
        name = ev.appliance_name or "Unknown Load"
        ts = _to_utc(ev.timestamp)
        watts = abs(float(ev.delta_power_w or 0.0))

        if ev.event_type == "TURN_ON":
            open_events.setdefault(name, []).append((ts, watts))
        elif ev.event_type == "TURN_OFF":
            if open_events.get(name):
                start_ts, start_watts = open_events[name].pop(0)
                duration_hrs = max((ts - start_ts).total_seconds() / 3600.0, 1.0 / 60.0)
                appliance_kwh[name] = appliance_kwh.get(name, 0.0) + (start_watts / 1000.0) * duration_hrs
            else:
                appliance_kwh[name] = appliance_kwh.get(name, 0.0) + (watts / 1000.0) * (1.0 / 60.0)

    for sess in active_sessions:
        name = sess.appliance_name or "Unknown Load"
        start_ts = _to_utc(sess.started_at)
        watts = abs(float(sess.power_draw_w or 0.0))
        if open_events.get(name):
            open_events[name].pop(0)
        duration_hrs = max((now_utc - start_ts).total_seconds() / 3600.0, 1.0 / 60.0)
        appliance_kwh[name] = appliance_kwh.get(name, 0.0) + (watts / 1000.0) * duration_hrs

    for name, pending in open_events.items():
        for start_ts, watts in pending:
            duration_hrs = max((now_utc - start_ts).total_seconds() / 3600.0, 1.0 / 60.0)
            appliance_kwh[name] = appliance_kwh.get(name, 0.0) + (watts / 1000.0) * duration_hrs

    sorted_appliances = sorted(appliance_kwh.items(), key=lambda x: x[1], reverse=True)
    disagg_labels = [k for k, v in sorted_appliances if v > 0]
    disagg_series = [round(v, 4) for k, v in sorted_appliances if v > 0]

    telemetry_rows = (
        db.query(TelemetryLog)
        .filter(TelemetryLog.device_id == device_id)
        .order_by(TelemetryLog.timestamp.asc(), TelemetryLog.id.asc())
        .all()
    )

    if range_mode == "7d":
        day_keys = [(now_ph - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(6, -1, -1)]
        day_labels = [(now_ph - timedelta(days=offset)).strftime("%b %d") for offset in range(6, -1, -1)]
        daily_kwh = {k: 0.0 for k in day_keys}

        for i in range(1, len(telemetry_rows)):
            prev_row = telemetry_rows[i - 1]
            curr_row = telemetry_rows[i]
            delta_kwh = max(0.0, float(curr_row.accumulated_kwh) - float(prev_row.accumulated_kwh))
            bucket = _to_utc(curr_row.timestamp).astimezone(PH_TZ).strftime("%Y-%m-%d")
            if bucket in daily_kwh:
                daily_kwh[bucket] += delta_kwh

        if sum(daily_kwh.values()) == 0.0 and events:
            for ev in events:
                if ev.event_type == "TURN_ON":
                    bucket = _to_utc(ev.timestamp).astimezone(PH_TZ).strftime("%Y-%m-%d")
                    if bucket in daily_kwh:
                        daily_kwh[bucket] += (abs(float(ev.delta_power_w or 0.0)) / 1000.0) * 0.25

        profile = {
            "range": "7d",
            "series_name": "Energy (kWh)",
            "unit": "kWh",
            "categories": day_labels,
            "data": [round(daily_kwh[k], 3) for k in day_keys],
        }
    else:
        hour_starts = [
            (now_ph.replace(minute=0, second=0, microsecond=0) - timedelta(hours=offset))
            for offset in range(23, -1, -1)
        ]
        hour_keys = [h.strftime("%Y-%m-%d %H") for h in hour_starts]
        hour_labels = [h.strftime("%I%p").lstrip("0") for h in hour_starts]
        hourly_kw = {k: 0.0 for k in hour_keys}

        for i in range(1, len(telemetry_rows)):
            prev_row = telemetry_rows[i - 1]
            curr_row = telemetry_rows[i]
            prev_ts = _to_utc(prev_row.timestamp)
            curr_ts = _to_utc(curr_row.timestamp)
            delta_kwh = max(0.0, float(curr_row.accumulated_kwh) - float(prev_row.accumulated_kwh))
            elapsed_hrs = max((curr_ts - prev_ts).total_seconds() / 3600.0, 1.0 / 60.0)
            avg_kw = delta_kwh / elapsed_hrs
            bucket = curr_ts.astimezone(PH_TZ).strftime("%Y-%m-%d %H")
            if bucket in hourly_kw:
                hourly_kw[bucket] = max(hourly_kw[bucket], avg_kw)

        running_kw = 0.0
        for ev in events:
            running_kw = max(0.0, running_kw + (float(ev.delta_power_w or 0.0) / 1000.0))
            bucket = _to_utc(ev.timestamp).astimezone(PH_TZ).strftime("%Y-%m-%d %H")
            if bucket in hourly_kw:
                hourly_kw[bucket] = max(hourly_kw[bucket], running_kw)

        current_hour_key = now_ph.strftime("%Y-%m-%d %H")
        active_kw = sum(float(s.power_draw_w or 0.0) for s in active_sessions) / 1000.0
        if current_hour_key in hourly_kw and active_kw > 0:
            hourly_kw[current_hour_key] = max(hourly_kw[current_hour_key], active_kw)

        profile = {
            "range": "24h",
            "series_name": "Power (kW)",
            "unit": "kW",
            "categories": hour_labels,
            "data": [round(hourly_kw[k], 3) for k in hour_keys],
        }

    return {
        "disaggregation": {
            "labels": disagg_labels,
            "series": disagg_series,
            "total_kwh": round(sum(disagg_series), 4),
        },
        "profile": profile,
    }


@app.get("/api/v1/providers", dependencies=[Depends(verify_api_key)])
def get_providers(db: Session = Depends(get_db)):
    providers = db.query(ProviderRule).order_by(ProviderRule.region.asc(), ProviderRule.provider_name.asc()).all()

    latest_rates: dict[str, ProviderRate] = {}
    all_rates = (
        db.query(ProviderRate)
        .order_by(ProviderRate.billing_month.asc(), ProviderRate.id.asc())
        .all()
    )
    for r in all_rates:
        latest_rates[r.provider_code] = r

    return [
        {
            "provider_code": p.provider_code,
            "provider_name": p.provider_name,
            "region": p.region,
            "base_distribution_fee": float(p.base_distribution_fee) if p.base_distribution_fee else 0.0,
            "benchmark_rate": round(float(latest_rates[p.provider_code].benchmark_rate), 4)
            if p.provider_code in latest_rates
            else None,
            "billing_month": latest_rates[p.provider_code].billing_month
            if p.provider_code in latest_rates
            else None,
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
            "confidence": float(e.confidence) if e.confidence is not None else 0.0,
            "timestamp": e.timestamp.replace(tzinfo=timezone.utc).isoformat() if getattr(e, "timestamp", None) else None
        }
        for e in events
    ]