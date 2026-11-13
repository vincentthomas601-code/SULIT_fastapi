import logging
from sqlalchemy.orm import Session
from database import Base, SessionLocal, engine
from models import ProviderRule, ProviderRate
from presyo_client import fetch_latest_available_month, parse_records

logger = logging.getLogger(__name__)


def scrape_utility_rates(exit_on_error: bool = True) -> dict:
    """
    Fetches the latest available utility rates from PRESYO-PH and updates
    ProviderRule and ProviderRate tables in the database.
    """
    if engine is None:
        msg = "Database engine is not initialized."
        logger.error(msg)
        if exit_on_error:
            raise RuntimeError(msg)
        return {"status": "error", "message": msg}

    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()

    try:
        billing_month, raw_records = fetch_latest_available_month()
        utilities, skipped = parse_records(raw_records)

        processed = 0
        for item in utilities:
            code = item["code"]
            name = item["name"]
            region = item["region"] or "Unknown"
            rate_val = item["rate"]

            provider = db.query(ProviderRule).filter(ProviderRule.provider_code == code).first()
            if not provider:
                provider = ProviderRule(
                    provider_code=code,
                    provider_name=name,
                    region=region,
                    base_distribution_fee=0.9803,
                    tier_1_max_kwh=200.0,
                    tier_1_rate=0.9803,
                    tier_2_max_kwh=300.0,
                    tier_2_rate=1.2908,
                    tier_3_max_kwh=400.0,
                    tier_3_rate=1.5837,
                    tier_4_rate=2.0941,
                )
                db.add(provider)
                db.flush()
            else:
                provider.provider_name = name
                if item["region"]:
                    provider.region = region

            rate_entry = (
                db.query(ProviderRate)
                .filter(
                    ProviderRate.provider_code == code,
                    ProviderRate.billing_month == billing_month,
                )
                .first()
            )
            if rate_entry:
                rate_entry.benchmark_rate = rate_val
                rate_entry.status = "SUCCESS"
            else:
                rate_entry = ProviderRate(
                    provider_code=code,
                    benchmark_rate=rate_val,
                    billing_month=billing_month,
                    status="SUCCESS",
                )
                db.add(rate_entry)

            processed += 1

        db.commit()
        return {
            "status": "success",
            "billing_month": billing_month,
            "records_processed": processed,
            "skipped_count": len(skipped),
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Rate sync failed: {e}")
        if exit_on_error:
            raise
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


if __name__ == "__main__":
    print(scrape_utility_rates(exit_on_error=False))