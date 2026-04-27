from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.models import Base


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Create the SQLAlchemy engine for the configured database."""
    resolved = settings or get_settings()
    connect_args: dict[str, object] = {}
    if resolved.is_sqlite:
        connect_args["check_same_thread"] = False
    return create_engine(resolved.database_url, future=True, connect_args=connect_args)


def create_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    """Create a configured session factory."""
    engine = create_db_engine(settings)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Provide a transactional session scope."""
    factory = create_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all_tables(settings: Settings | None = None) -> None:
    """Create all database tables from SQLAlchemy metadata."""
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
