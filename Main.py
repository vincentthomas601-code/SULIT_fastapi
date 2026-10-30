import io
import re
from typing import Optional
import pdfplumber
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from database import Base, engine, get_db
from models import ProviderRule, ProviderRate, UserSettings, TelemetryLog
from billing_engine import calculate_ph_electricity_bill, get_energy_insights
from scraper import start_scheduler, scrape_utility_rates
from sqlalchemy import text

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

class TelemetryInput(BaseModel):
    device_id: str = Field(..., example="ESP32_01")
    kwh: float = Field(..., example=310.0)

class UserSettingsUpdate(BaseModel):
    selected_provider: Optional[str] = None
    use_custom_rate: Optional[bool] = None
    custom_rate: Optional[float] = None

class InsightRequest(BaseModel):
    device_id: str
    prompt_text: str


def get_or_create_device_settings(device_id: str, db: Session) -> UserSettings:
    """Helper function to fetch or create settings tied exclusively to a device_id."""
    settings = db.query(UserSettings).filter(UserSettings.device_id == device_id).first()
    if not settings:
        settings = UserSettings(
            device_id=device_id,
            selected_provider="MERALCO",
            use_custom_rate=False
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@app.on_event("startup")
def startup_event():
    # Auto-migrate missing columns for existing SQLite database file
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE user_settings ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))
            conn.commit()
        except Exception:
            pass
            
        try:
            conn.execute(text("ALTER TABLE telemetry_logs ADD COLUMN device_id VARCHAR DEFAULT 'default_device'"))
            conn.commit()
        except Exception:
            pass

    Base.metadata.create_all(bind=engine)
    db = next(get_db())

    # Initial sync of all PRESYO-PH providers on startup if database is empty
    if not db.query(ProviderRule).first():
        scrape_utility_rates()

    start_scheduler()


@app.get("/")
def serve_dashboard():
    """Serves index.html UI at root URL."""
    return FileResponse("index.html")


@app.post("/api/v1/telemetry")
def ingest_telemetry(data: TelemetryInput, db: Session = Depends(get_db)):
    log_entry = TelemetryLog(device_id=data.device_id, accumulated_kwh=data.kwh)
    db.add(log_entry)
    db.commit()
    return {"status": "SUCCESS", "message": "Telemetry log stored successfully."}


@app.get("/api/v1/billing/live")
def get_live_billing(
    x_device_id: str = Header("default_device", alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
    settings = get_or_create_device_settings(x_device_id, db)

    # Fetch latest telemetry log for this device
    latest_telemetry = (
        db.query(TelemetryLog)
        .filter(TelemetryLog.device_id == x_device_id)
        .order_by(TelemetryLog.id.desc())
        .first()
    )

    # TEMPORARY PLACEHOLDER: Auto-insert a 310.0 kWh fake ESP32 database entry if no log exists
    if not latest_telemetry:
        latest_telemetry = TelemetryLog(device_id=x_device_id, accumulated_kwh=310.0)
        db.add(latest_telemetry)
        db.commit()
        db.refresh(latest_telemetry)

    accumulated_kwh = latest_telemetry.accumulated_kwh

    provider_code = settings.selected_provider or "MERALCO"
    rule = db.query(ProviderRule).filter(ProviderRule.provider_code == provider_code).first()
    if not rule:
        rule = db.query(ProviderRule).first()

    if settings.use_custom_rate and settings.custom_rate and settings.custom_rate > 0:
        active_rate = settings.custom_rate
        rate_status = "CUSTOM_OVERRIDE"
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

    # Construct complete payload expected by index.html
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
    x_device_id: str = Header("default_device", alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
    settings = get_or_create_device_settings(x_device_id, db)

    if payload.selected_provider is not None:
        settings.selected_provider = payload.selected_provider
    if payload.use_custom_rate is not None:
        settings.use_custom_rate = payload.use_custom_rate
    if payload.custom_rate is not None:
        settings.custom_rate = payload.custom_rate

    db.commit()
    return {"status": "SUCCESS", "message": f"User settings updated successfully for device {x_device_id}."}


@app.post("/api/v1/billing/upload-ebill")
async def upload_ebill(
    file: UploadFile = File(...),
    x_device_id: str = Header("default_device", alias="X-Device-ID"),
    db: Session = Depends(get_db)
):
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

        # Apply settings update exclusively to the uploader's device_id
        settings = get_or_create_device_settings(x_device_id, db)
        settings.use_custom_rate = True
        settings.custom_rate = effective_rate
        db.commit()

        return {
            "status": "SUCCESS",
            "parsed_amount_php": total_amount,
            "parsed_kwh": total_kwh,
            "calculated_effective_rate_php": effective_rate,
            "message": f"Successfully parsed e-Bill. Effective rate set to ₱{effective_rate}/kWh for device {x_device_id}."
        }

    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Failed to process PDF: {str(err)}")


@app.post("/api/insights")
def generate_insights(payload: InsightRequest, db: Session = Depends(get_db)):
    settings = get_or_create_device_settings(payload.device_id, db)

    try:
        report = get_energy_insights(payload.prompt_text)
        return {"status": "success", "insights": report}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))