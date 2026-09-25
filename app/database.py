from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings


is_sqlite = settings.database_url.startswith("sqlite")
# A webhook's DB transaction stays open across the awaited Deriv API call
# inside place_order()/close_order() (can take several seconds), so a
# second signal arriving in that window - e.g. the explicit close-then-enter
# pair a reversal alert fires back to back - needs SQLite to wait for the
# lock instead of raising "database is locked" immediately. The 5s
# default has been observed to be too short for that; give it real headroom.
connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
# Several endpoints (e.g. /api/open-positions) hold their DB session open
# across awaited, per-position Deriv network calls that can take seconds
# each. Under FastAPI + sync SQLAlchemy, a QueuePool's checkout wait for an
# already-exhausted pool blocks synchronously right on the asyncio event
# loop thread, freezing the *entire* app (every route, not just DB ones)
# until it times out - this is what surfaces as Railway's "Application
# failed to respond". NullPool sidesteps that: each checkout just opens its
# own sqlite3 connection (cheap) instead of queueing for a shared, limited
# pool, so a slow request can never block anyone else's checkout.
pool_kwargs = {"poolclass": NullPool} if is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=connect_args, **pool_kwargs)

if is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        # WAL lets readers (the dashboard's frequent GETs) proceed without
        # waiting on an in-progress writer, on top of the busy timeout above.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def _sql_literal(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def sync_schema() -> None:
    """Add columns that exist on the ORM models but not yet in the DB table.

    Base.metadata.create_all only creates missing tables — it never alters
    existing ones, so a table added before a column existed is stuck without
    it until we backfill it here.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(dialect=engine.dialect)}'
                if column.default is not None and column.default.is_scalar:
                    ddl += f" DEFAULT {_sql_literal(column.default.arg)}"
                    if not column.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
