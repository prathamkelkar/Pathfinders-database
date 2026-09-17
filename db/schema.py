"""
Schema creation entry point. Run this to create all tables defined in db/models.py
against a given database URL (defaults to the local SQLite file).
"""

from db.models import Base
from db.session import DEFAULT_DB_URL, get_engine


def create_all(db_url: str = DEFAULT_DB_URL) -> None:
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    create_all()
