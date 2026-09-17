"""
Engine/session setup. SQLite by default (see README) — swapping to Postgres later is
a connection-string change since the schema avoids SQLite-specific features.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DB_URL = "sqlite:///debt_policy.db"


def get_engine(db_url: str = DEFAULT_DB_URL):
    return create_engine(db_url)


def get_session(db_url: str = DEFAULT_DB_URL) -> Session:
    engine = get_engine(db_url)
    return sessionmaker(bind=engine)()
