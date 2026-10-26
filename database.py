from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# 1. Define the SQLite Database URL
SQLALCHEMY_DATABASE_URL = "sqlite:///./nilm_billing.db"

# 2. Initialize the Database Engine
# 'check_same_thread': False allows multiple threads in FastAPI to access SQLite safely
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False}
)

# 3. Create a Session Factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 4. Declarative Base Class for Models
Base = declarative_base()

# 5. Database Session Dependency for FastAPI
def get_db():
    """
    Yields an active database session for an incoming HTTP request,
    and guarantees connection teardown when the request completes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()