import sys
import logging
import re
from datetime import datetime
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from playwright.sync_api import sync_playwright

from database import Base, engine, SessionLocal
from models import ProviderRate, ProviderRule
from presyo_client import current_month_ph, extract_provider_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ScraperWorker")

PRESYO_NATIONAL_URL = "https://presyo.icsc.ngo/national"

def scrape_presyo_with_browser():
    """Uses Playwright to render JavaScript and extract the fully loaded table."""
    utilities = {}
    published_month = current_month_ph()

    with sync_playwright() as p:
        # headless=True runs the browser silently in the background
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        try:
            page.goto(PRESYO_NATIONAL_URL, wait_until="networkidle", timeout=30000)
            # Wait specifically for the data table rows to appear in the DOM
            page.wait_for_selector("table tbody tr", timeout=15000)
            
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            
            # Extract the published target month from the period dropdown on the page
            dropdown = soup.select_one("button[role='combobox']")
            if dropdown and dropdown.text:
                try:
                    published_month = datetime.strptime(dropdown.text.strip(), "%B %Y").strftime("%Y-%m")
                except ValueError:
                    pass
            
            # Parse the fully rendered HTML table
            for row in soup.select("table tbody tr"):
                cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
                if len(cells) < 4:
                    continue
                    
                du_cell = cells[1]
                grid_cell = cells[2] or "On-grid"
                rate_cell = cells[3]
                
                rate_match = re.search(r"(\d+\.\d+)", rate_cell.replace(",", ""))
                if not rate_match:
                    continue
                
                rate_val = float(rate_match.group(1))
                
                # Split provider acronym from its full name (e.g. "MERALCO Manila Electric...")
                acronym_match = re.match(r"^([A-Z0-9\-]+(?:\s+(?:AREA\s*\d+|A\d+|\d+))?)\s+(.+)$", du_cell)
                raw_code = acronym_match.group(1).strip() if acronym_match else du_cell
                
                code = extract_provider_code(raw_code)
                utilities[code] = {
                    "code": code,
                    "name": acronym_match.group(2).strip() if acronym_match else du_cell,
                    "region": grid_cell,
                    "rate": rate_val,
                    "fee": 0.0,
                }
                
        finally:
            browser.close()

    if not utilities:
        raise RuntimeError("Browser scraper extracted 0 utilities. The table may be missing or the site structure changed.")
        
    logger.info(f"Successfully scraped {len(utilities)} utilities for {published_month}.")
    return published_month, list(utilities.values())

def _apply_database_fallback(db: Session, target_month: str):
    """If the live fetch fails, fallback to the latest stored rate for each provider."""
    providers = db.query(ProviderRule).all()
    carried_forward = 0

    for provider in providers:
        # Find the most recent rate for this specific provider
        latest_rate = (db.query(ProviderRate)
            .filter(ProviderRate.provider_code == provider.provider_code)
            .order_by(ProviderRate.billing_month.desc(), ProviderRate.id.desc())
            .first())
        
        if not latest_rate:
            continue

        # Only fallback if a rate doesn't already exist for the target month
        existing = db.query(ProviderRate).filter(
            ProviderRate.provider_code == provider.provider_code,
            ProviderRate.billing_month == target_month
        ).first()
        
        if not existing:
            db.add(ProviderRate(
                provider_code=provider.provider_code,
                benchmark_rate=latest_rate.benchmark_rate,
                billing_month=target_month,
                status="FALLBACK_LATEST",
            ))
        carried_forward += 1

    db.commit()
    logger.warning(f"Fallback applied for {carried_forward} providers ({target_month}).")

def scrape_utility_rates(exit_on_error: bool = True):
    logger.info("Executing Playwright PRESYO-PH sync job...")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    
    try:
        billing_month, utilities = scrape_presyo_with_browser()
        
        # Remove any stale fallback rows for this specific month before inserting the real data
        db.query(ProviderRate).filter(
            ProviderRate.status == "FALLBACK_LATEST",
            ProviderRate.billing_month == billing_month
        ).delete()

        for util in utilities:
            code, name, region, rate = util["code"], util["name"], util["region"], util["rate"]
            
            # Create rule if it doesn't exist yet
            if not db.query(ProviderRule).filter_by(provider_code=code).first():
                db.add(ProviderRule(provider_code=code, provider_name=name, region=region, base_distribution_fee=0.0))

            # Update existing rate or create a new successful row
            existing = db.query(ProviderRate).filter_by(provider_code=code, billing_month=billing_month).first()
            if existing:
                existing.benchmark_rate = rate
                existing.status = "SUCCESS"
            else:
                db.add(ProviderRate(provider_code=code, benchmark_rate=rate, billing_month=billing_month, status="SUCCESS"))

        db.commit()
        logger.info(f"Database synced with PRESYO rates for {billing_month}.")
        
    except Exception as err:
        logger.error(f"Live fetch failed: {err}")
        db.rollback()
        _apply_database_fallback(db, current_month_ph())
        if exit_on_error:
            sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    scrape_utility_rates(exit_on_error=True)