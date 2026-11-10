import os
import sys
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("Database")

DATABASE_URL = os.getenv("DATABASE_URL")
IS_PRODUCTION = os.getenv("RENDER", "false").lower() == "true" or os.getenv("ENV") == "production"

if not DATABASE_URL:
    if IS_PRODUCTION:
        logger.critical("DATABASE_URL environment variable is missing in production environment!")
        sys.exit(1)
    DATABASE_URL = "sqlite:///./nilm_billing.db"
    logger.warning("No DATABASE_URL set. Falling back to local SQLite database: nilm_billing.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

try:
    engine = create_engine(
        DATABASE_URL, 
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=1800 if not DATABASE_URL.startswith("sqlite") else -1
    )
    # Test DB Connection
    with engine.connect() as conn:
        pass
except Exception as err:
    logger.critical(f"Fatal error connecting to database at {DATABASE_URL}: {err}")
    if IS_PRODUCTION or not DATABASE_URL.startswith("sqlite"):
        sys.exit(1)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()