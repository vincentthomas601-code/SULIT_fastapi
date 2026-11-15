import calendar
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin
import requests

PRESYO_BASE_URL = "https://presyo.icsc.ngo"
PRESYO_API_URL = os.getenv("PRESYO_API_URL", f"{PRESYO_BASE_URL}/api/national")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("PRESYO_TIMEOUT", "30"))
LOOKBACK_MONTHS = int(os.getenv("PRESYO_LOOKBACK_MONTHS", "6"))
USER_AGENT = "SULIT-PRO-rate-sync/1.0 (+residential rate monitoring)"
PH_TZ = timezone(timedelta(hours=8))


def current_month_ph() -> str:
    return datetime.now(PH_TZ).strftime("%Y-%m")


def fetch_month(year: int, month: int) -> Any:
    params = {"month": calendar.month_name[month], "year": year}
    response = requests.get(
        PRESYO_API_URL,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return response.json()


def _unwrap_payload(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "records", "results", "utilities", "items", "rows", "providers"):
            val = payload.get(key)
            if isinstance(val, list) and val and all(isinstance(i, dict) for i in val):
                return val
        candidates = [
            v for v in payload.values()
            if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
        ]
        if len(candidates) == 1:
            return candidates[0]
        # Handle dict-of-dicts keyed by utility code (e.g. {"MERALCO": {...}})
        dict_entries = [
            {"code": k, **v}
            for k, v in payload.items()
            if isinstance(v, dict) and any(
                rk in v for rk in ("rate", "residential_rate", "total_rate", "price", "name", "utility")
            )
        ]
        if dict_entries:
            return dict_entries
    return []


def extract_records(
    payload: Any,
    params: Optional[Dict[str, Any]] = None,
    base_url: str = PRESYO_API_URL,
    depth: int = 0,
) -> List[Dict[str, Any]]:
    if payload is None or depth > 3:
        return []

    records = _unwrap_payload(payload)

    # Follow API directory links and try all utility-related endpoints
    if records and "href" in records[0]:
        prioritized = sorted(
            records,
            key=lambda item: 0
            if any(
                k in f"{item.get('endpoint', '')} {item.get('label', '')}".lower()
                for k in ("distribution", "utility", "utilities", "du", "rate", "national", "provider")
            )
            else 1,
        )
        for item in prioritized:
            href = item.get("href")
            if not href:
                continue
            target_url = urljoin(base_url, str(href))
            try:
                response = requests.get(
                    target_url,
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                sub_records = extract_records(
                    response.json(), params=params, base_url=target_url, depth=depth + 1
                )
                if sub_records:
                    parse_records(sub_records)
                    return sub_records
            except Exception:
                continue
        return []

    return records


def _fetch_via_playwright() -> Tuple[str, List[Dict[str, Any]]]:
    """Fallback: renders presyo.icsc.ngo/national with Playwright and extracts utility rows."""
    from playwright.sync_api import sync_playwright

    captured_json: List[List[Dict[str, Any]]] = []
    rendered_records: List[Dict[str, Any]] = []
    billing_month = current_month_ph()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)

            def on_response(resp):
                try:
                    if "/api/" in resp.url and resp.status == 200:
                        data = resp.json()
                        recs = _unwrap_payload(data)
                        if recs and "href" not in recs[0]:
                            captured_json.append(recs)
                except Exception:
                    pass

            page.on("response", on_response)
            page.goto(f"{PRESYO_BASE_URL}/national", wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)

            for batch in captured_json:
                try:
                    parse_records(batch)
                    return billing_month, batch
                except Exception:
                    continue

            rows = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('table').forEach(table => {
                    const headers = Array.from(table.querySelectorAll('thead th, tr th'))
                        .map(th => th.innerText.trim().toLowerCase());

                    let nameIdx = headers.findIndex(h =>
                        h.includes('utility') || h.includes('provider') || h.includes('du') || h.includes('name')
                    );
                    let regionIdx = headers.findIndex(h =>
                        h.includes('grid') || h.includes('region') || h.includes('area')
                    );
                    let rateIdx = headers.findIndex(h =>
                        (h.includes('rate') || h.includes('residential') || h.includes('kwh') || h.includes('php') || h.includes('peso')) &&
                        !h.includes('date') && !h.includes('verified') && !h.includes('change')
                    );

                    if (nameIdx < 0) nameIdx = 0;

                    table.querySelectorAll('tbody tr').forEach(tr => {
                        const cells = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
                        if (cells.length < 2) return;

                        let rateVal = rateIdx >= 0 && rateIdx < cells.length ? cells[rateIdx] : null;
                        if (!rateVal) {
                            for (let i = 1; i < cells.length; i++) {
                                if (i === regionIdx) continue;
                                const cleaned = cells[i].replace(/[^0-9.-]/g, '');
                                const num = parseFloat(cleaned);
                                if (!isNaN(num) && num >= 3.0 && num <= 35.0 && !cells[i].includes('202')) {
                                    rateVal = cells[i];
                                    break;
                                }
                            }
                        }

                        if (cells[nameIdx] && rateVal) {
                            out.push({
                                name: cells[nameIdx],
                                region: regionIdx >= 0 && regionIdx < cells.length ? cells[regionIdx] : 'Luzon Grid',
                                rate: rateVal
                            });
                        }
                    });
                });
                return out;
            }""")
            if rows:
                rendered_records = rows
        finally:
            browser.close()

    if not rendered_records:
        raise RuntimeError("Playwright could not extract utility rows from PRESYO-PH.")
    return billing_month, rendered_records


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
            params = {"month": calendar.month_name[month], "year": year}
            records = extract_records(fetch_month(year, month), params=params)
            if records:
                parse_records(records)
                found.append((f"{year:04d}-{month:02d}", records))
        except Exception:
            continue
    if not found:
        bm, recs = _fetch_via_playwright()
        found.append((bm, recs))
    return found


def fetch_latest_available_month() -> Tuple[str, List[Dict[str, Any]]]:
    """Returns (billing_month, raw_records) for the most recent month published on PRESYO-PH."""
    last_err: Optional[Exception] = None
    for year, month in _months_back_from_now():
        try:
            params = {"month": calendar.month_name[month], "year": year}
            records = extract_records(fetch_month(year, month), params=params)
            if records:
                parse_records(records)
                return f"{year:04d}-{month:02d}", records
        except Exception as exc:
            last_err = exc
            continue

    try:
        return _fetch_via_playwright()
    except Exception as pw_exc:
        raise RuntimeError(
            f"PRESYO-PH returned no valid records for the last {LOOKBACK_MONTHS} months "
            f"(api_err={last_err}, playwright_err={pw_exc})."
        )


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
        "name": resolve(
            "PRESYO_FIELD_NAME",
            ["utility", "utility_name", "name", "du_name", "distribution_utility", "provider", "code"],
        ),
        "rate": resolve(
            "PRESYO_FIELD_RATE",
            [
                "rate",
                "residential_rate",
                "total_rate",
                "price",
                "effective_rate",
                "residential",
                "kwh_rate",
                "all_in_rate",
                "rates",
            ],
        ),
        "region": resolve("PRESYO_FIELD_REGION", ["grid", "region", "grid_classification", "area"]),
        "generation": resolve("PRESYO_FIELD_GENERATION", ["generation", "generation_rate", "gen_rate"]),
    }
    missing = [k for k in ("name", "rate") if not mapping[k]]
    if missing:
        raise RuntimeError(f"Missing field mappings for {missing}. Available keys: {sorted(keys)}")
    return mapping


def parse_number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("no numeric value")
    if isinstance(value, dict):
        for k in ("residential", "total", "effective", "rate", "amount", "value", "php", "200kwh"):
            if k in value and value[k] is not None:
                return parse_number(value[k])
        for v in value.values():
            try:
                num = parse_number(v)
                if 3.0 <= num <= 35.0:
                    return num
            except ValueError:
                continue
        raise ValueError(f"no numeric rate in dict {value!r}")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        cleaned = re.sub(r"[^\d.\-]", "", str(value))
        if not cleaned or cleaned in (".", "-", "-."):
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
        explicit = record.get("code") or record.get("acronym") or record.get("short_name")
        if explicit and str(explicit).strip():
            return slugify(str(explicit))

    if "MERALCO" in name.upper():
        return "MERALCO"

    paren_match = re.search(r"\(([A-Za-z0-9\-]+)\)", name)
    if paren_match:
        return slugify(paren_match.group(1))

    return slugify(name)[:24]


def parse_records(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not records:
        raise RuntimeError("Empty records list passed to parse_records.")

    mapping = field_map(records[0])
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
        raise RuntimeError(
            f"No valid utilities parsed from {len(records)} records. "
            f"Sample keys={list(records[0].keys())}, sample_row={records[0]!r}, errors={skipped[:5]}"
        )
    return list(utilities.values()), skipped