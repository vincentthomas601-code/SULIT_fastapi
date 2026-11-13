import os
import logging
from urllib.parse import urlparse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger(__name__)

raw_db_url = os.getenv("DATABASE_URL", "sqlite:///./sulit_pro.db")

if raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif raw_db_url.startswith("postgresql://") and not raw_db_url.startswith("postgresql+psycopg"):
    DATABASE_URL = raw_db_url.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = raw_db_url


def get_safe_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.password:
            safe_netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
            return parsed._replace(netloc=safe_netloc).geturl()
    except Exception:
        pass
    return url


Base = declarative_base()

engine = None
try:
    if DATABASE_URL.startswith("sqlite"):
        engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with engine.connect() as conn:
        pass

except Exception as e:
    safe_url = get_safe_url(DATABASE_URL)
    logger.error(f"Failed to connect to database at {safe_url}. Error: {e}")
    logger.info("Falling back to SQLite database.")
    DATABASE_URL = "sqlite:///./sulit_pro_fallback.db"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def ensure_database_columns():
    """Ensure all required columns exist without failing PostgreSQL transaction blocks."""
    if engine is None:
        logger.error("Database engine is not initialized. Cannot ensure columns.")
        return

    alter_statements = [
        "ALTER TABLE user_settings ADD COLUMN device_id VARCHAR DEFAULT 'default_device'",
        "ALTER TABLE user_settings ADD COLUMN monthly_budget_php FLOAT DEFAULT NULL",
        "ALTER TABLE user_settings ADD COLUMN cycle_start_kwh FLOAT DEFAULT 0.0",
        "ALTER TABLE telemetry_logs ADD COLUMN device_id VARCHAR DEFAULT 'default_device'",
    ]

    for stmt in alter_statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            # Column already exists or table not yet created
            pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()