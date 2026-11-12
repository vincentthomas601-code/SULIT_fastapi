import calendar
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import requests

PRESYO_API_URL = os.getenv("PRESYO_API_URL", "https://presyo.icsc.ngo/api/national")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("PRESYO_TIMEOUT", "30"))
LOOKBACK_MONTHS = int(os.getenv("PRESYO_LOOKBACK_MONTHS", "6"))
USER_AGENT = "SULIT-PRO-rate-sync/1.0 (+residential rate monitoring)"
PH_TZ = timezone(timedelta(hours=8))


def current_month_ph() -> str:
    return datetime.now(PH_TZ).strftime("%Y-%m")


def fetch_month(year: int, month: int) -> Any:
    response = requests.get(
        PRESYO_API_URL,
        params={"month": calendar.month_name[month], "year": year},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return response.json()


def extract_records(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        candidates = [
            v for v in payload.values()
            if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError("Ambiguous API response.")
        return []
    raise RuntimeError(f"Unexpected API response type: {type(payload).__name__}")


def _months_back_from_now():
    now = datetime.now(PH_TZ)
    year, month = now.year, now.month
    for _ in range(LOOKBACK_MONTHS):
        yield year, month
        month -= 1
        if month == 0:
            month, year = 12, year - 1


def fetch_recent_months() -> List[Tuple[str, List[Dict[str, Any]]]]:
    found = []
    for year, month in _months_back_from_now():
        try:
            records = extract_records(fetch_month(year, month))
            if records:
                found.append((f"{year:04d}-{month:02d}", records))
        except Exception:
            continue
    if not found:
        raise RuntimeError(f"PRESYO-PH returned no records for the last {LOOKBACK_MONTHS} months.")
    return found


def fetch_latest_available_month() -> Tuple[str, List[Dict[str, Any]]]:
    """Returns (billing_month, raw_records) for the most recent month published on PRESYO-PH."""
    for year, month in _months_back_from_now():
        try:
            records = extract_records(fetch_month(year, month))
            if records:
                return f"{year:04d}-{month:02d}", records
        except Exception:
            continue
    raise RuntimeError(f"PRESYO-PH returned no records for the last {LOOKBACK_MONTHS} months.")


def field_map(sample_record: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[str]]:
    keys = set(sample_record.keys()) if sample_record else set()

    def resolve(env_key: str, candidates: List[str]) -> Optional[str]:
        env_val = os.getenv(env_key)
        if env_val:
            return env_val
        for candidate in candidates:
            if candidate in keys:
                return candidate
        return None

    mapping = {
        "name": resolve("PRESYO_FIELD_NAME", ["utility", "name", "du_name", "distribution_utility", "code"]),
        "rate": resolve("PRESYO_FIELD_RATE", ["rate", "residential_rate", "total_rate", "price"]),
        "region": resolve("PRESYO_FIELD_REGION", ["grid", "region", "grid_classification"]),
        "generation": resolve("PRESYO_FIELD_GENERATION", ["generation", "generation_rate", "gen_rate"]),
    }
    missing = [k for k in ("name", "rate") if not mapping[k]]
    if missing:
        raise RuntimeError(f"Missing field mappings for {missing}. Available keys: {sorted(keys)}")
    return mapping


def parse_number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("no numeric value")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        cleaned = re.sub(r"[^\d.\-]", "", str(value))
        if not cleaned:
            raise ValueError(f"no numeric value in {value!r}")
        number = float(cleaned)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def slugify(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def extract_provider_code(name: str, record: Optional[Dict[str, Any]] = None) -> str:
    """Extracts a clean provider code (e.g. 'MERALCO' from 'Manila Electric Company (MERALCO)')."""
    if record:
        explicit = record.get("code") or record.get("acronym")
        if explicit and str(explicit).strip():
            return slugify(str(explicit))

    if "MERALCO" in name.upper():
        return "MERALCO"

    paren_match = re.search(r"\(([A-Za-z0-9\-]+)\)", name)
    if paren_match:
        return slugify(paren_match.group(1))

    return slugify(name)[:24]


def parse_records(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    mapping = field_map(records[0])
    for required in ("name", "rate"):
        if mapping[required] not in records[0]:
            raise RuntimeError(f"Field '{mapping[required]}' not found.")

    utilities: Dict[str, Dict[str, Any]] = {}
    skipped: List[str] = []

    for record in records:
        raw_name = record.get(str(mapping["name"]))
        try:
            if not raw_name or not str(raw_name).strip():
                raise ValueError("empty utility name")
            name = str(raw_name).strip()
            rate = parse_number(record.get(str(mapping["rate"])))
            if not (3.0 <= rate <= 35.0):
                raise ValueError(f"out-of-bounds rate {rate}")

            generation = None
            if mapping["generation"]:
                raw_gen = record.get(str(mapping["generation"]))
                if raw_gen not in (None, ""):
                    generation = parse_number(raw_gen)

            region = "Luzon Grid"
            if mapping["region"]:
                raw_region = record.get(str(mapping["region"]))
                if raw_region:
                    region = str(raw_region).strip()

            code = extract_provider_code(name, record)
            if not code:
                raise ValueError("utility name has no usable characters")

            utilities[code] = {
                "code": code,
                "name": name,
                "region": region,
                "rate": rate,
                "generation_rate": generation,
                "fee": 0.0,
            }
        except ValueError as err:
            skipped.append(f"{raw_name!r}: {err}")

    if not utilities:
        raise RuntimeError("No valid utilities parsed.")
    return list(utilities.values()), skipped