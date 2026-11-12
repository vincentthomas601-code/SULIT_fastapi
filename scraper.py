import sys
import json
import logging
from datetime import datetime
from typing import Dict, List, Any
import requests
from bs4 import BeautifulSoup
from database import Base, engine, SessionLocal
from models import ProviderRate, ProviderRule

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ScraperWorker")

PRESYO_NATIONAL_URL = "https://presyo.icsc.ngo/national"

def scrape_all_presyo_utilities() -> List[Dict[str, Any]]:
    scraped_utilities = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(PRESYO_NATIONAL_URL, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            next_data_tag = soup.find("script", {"id": "__NEXT_DATA__"})
            
            if next_data_tag and next_data_tag.string:
                data_json = json.loads(next_data_tag.string)
                page_props = data_json.get("props", {}).get("pageProps", {})
                rates_list = page_props.get("rates", []) or page_props.get("utilities", [])
                
                for item in rates_list:
                    try:
                        raw_rate = item.get("rate") or item.get("residential_rate", 0.0)
                        
                        # FIXED: Strip currency symbols, commas, and whitespace before casting
                        clean_rate_str = str(raw_rate).replace("₱", "").replace("PHP", "").replace(",", "").strip()
                        rate_val = float(clean_rate_str)

                        # Validate Philippines residential rate bounds (₱5.00 to ₱30.00/kWh)
                        if not (5.0 <= rate_val <= 30.0):
                            logger.warning(f"Skipping out-of-bounds rate item: {item}")
                            continue

                        scraped_utilities.append({
                            "code": (item.get("code") or item.get("acronym") or item.get("utility", "UNKNOWN")[:8]).upper(),
                            "name": item.get("name") or item.get("utility", "Unknown Utility"),
                            "region": item.get("grid") or item.get("region", "Luzon Grid"),
                            "rate": rate_val,
                            "fee": 0.0
                        })
                    except (ValueError, TypeError) as parse_err:
                        logger.warning(f"Failed to parse item {item}: {parse_err}")
                        continue
    except Exception as err:
        logger.error(f"[PRESYO-PH Scraper] Live scrape failed: {err}")

    if not scraped_utilities:
        raise RuntimeError("Scraper extracted 0 valid utilities. Scrape failed validation.")

    logger.info(f"Successfully validated and scraped {len(scraped_utilities)} utilities.")
    return scraped_utilities

def scrape_utility_rates():
    logger.info("Executing full PRESYO-PH rate sync job...")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    current_month = datetime.now().strftime("%Y-%m")

    try:
        utilities = scrape_all_presyo_utilities()

        for util in utilities:
            code = util["code"]
            name = util["name"]
            region = util["region"]
            rate = util["rate"]
            fee = util.get("fee", 0.0)

            rule = db.query(ProviderRule).filter(ProviderRule.provider_code == code).first()
            if not rule:
                rule = ProviderRule(
                    provider_code=code,
                    provider_name=name,
                    region=region,
                    base_distribution_fee=fee
                )
                db.add(rule)

            # Upsert ProviderRate entry
            existing_rate = db.query(ProviderRate).filter(
                ProviderRate.provider_code == code,
                ProviderRate.billing_month == current_month
            ).first()

            if existing_rate:
                existing_rate.benchmark_rate = rate  # type: ignore
                existing_rate.status = "SUCCESS"     # type: ignore
            else:
                rate_entry = ProviderRate(
                    provider_code=code,
                    benchmark_rate=rate,
                    billing_month=current_month,
                    status="SUCCESS"
                )
                db.add(rate_entry)

        db.commit()
        logger.info(f"Database successfully synchronized with {len(utilities)} PRESYO rates.")
    except Exception as err:
        logger.critical(f"Fatal error during PRESYO-PH sync: {err}")
        db.rollback()
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    scrape_utility_rates()