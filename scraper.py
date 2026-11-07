import logging
import re
import json
from datetime import datetime
from typing import Dict, List, Any
import requests
from bs4 import BeautifulSoup 
from apscheduler.schedulers.background import BackgroundScheduler
from database import Base, engine, SessionLocal
from models import ProviderRate, ProviderRule

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ScraperWorker")

PRESYO_NATIONAL_URL = "https://presyo.icsc.ngo/national"

def scrape_all_presyo_utilities() -> List[Dict[str, Any]]:
    """
    Scrapes PRESYO-PH national portal.
    Attempts parsing live dynamic payloads.
    """
    scraped_utilities = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(PRESYO_NATIONAL_URL, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Parse Next.js __NEXT_DATA__ payload tag if rendered server-side
            next_data_tag = soup.find("script", {"id": "__NEXT_DATA__"})
            if next_data_tag and next_data_tag.string:
                data_json = json.loads(next_data_tag.string)
                page_props = data_json.get("props", {}).get("pageProps", {})
                rates_list = page_props.get("rates", []) or page_props.get("utilities", [])
                for item in rates_list:
                    if "utility" in item or "name" in item:
                        scraped_utilities.append({
                            "code": item.get("code") or item.get("acronym") or item.get("utility", "")[:8].upper(),
                            "name": item.get("name") or item.get("utility"),
                            "region": item.get("grid") or item.get("region", "Unknown"),
                            "rate": float(item.get("rate") or item.get("residential_rate", 0.0)),
                            "fee": 0.0
                        })

    except Exception as err:
        logger.warning(f"[PRESYO-PH Scraper] Live web scrape encountered issue: {err}")

    logger.info(f"[PRESYO-PH Scraper] Scraped {len(scraped_utilities)} utilities from live portal.")
    return scraped_utilities


def scrape_utility_rates():
    """
    Monthly background worker job.
    Auto-creates missing database tables and synchronizes all PRESYO-PH utilities.
    """
    logger.info("Executing full PRESYO-PH discovery & rate sync job...")
    Base.metadata.create_all(bind=engine)  # Automatically creates missing SQLite tables
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

            # Register or update ProviderRule
            rule = db.query(ProviderRule).filter(ProviderRule.provider_code == code).first()
            if not rule:
                rule = ProviderRule(
                    provider_code=code,
                    provider_name=name,
                    region=region,
                    base_distribution_fee=fee
                )
                db.add(rule)

            # Record monthly benchmark rate entry
            rate_entry = ProviderRate(
                provider_code=code,
                benchmark_rate=rate,
                billing_month=current_month,
                status="SUCCESS"
            )
            db.add(rate_entry)

        db.commit()
        logger.info(f"Successfully synchronized {len(utilities)} PRESYO-PH utilities to database.")

    except Exception as err:
        logger.error(f"Fatal error during PRESYO-PH sync: {err}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    """Starts background APScheduler worker."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_utility_rates, "cron", day=10, hour=2, minute=0)
    scheduler.start()
    logger.info("APScheduler PRESYO-PH worker initialized.")