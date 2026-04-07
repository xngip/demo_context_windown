from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    with engine.begin() as conn:
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS unaccent'))
    Base.metadata.create_all(bind=engine)
