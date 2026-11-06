from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import settings
from backend.tenant_auth import resolve_owner_sub

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 10},
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db(request: Request = None):
    """Request-gebundene Session mit Tenant-Bindung (fail-closed).

    Der Saganta-Weg (kalender-bff/app-proxy) stempelt die better-auth-sub als
    X-Saganta-Sub-Header. Fehlt der Header (natives Frontend/JWT, Feed-Token,
    iCal, KG-internal, mobile) faellt das Scoping auf DEFAULT_OWNER_SUB zurueck
    = bisheriges Single-User-Verhalten. Das globale do_orm_execute/before_flush
    (backend/tenant.py) liest session.info["owner_sub"].

    Die Echtheit des Headers prueft backend/tenant_auth.py: bewusst VOR
    SessionLocal(), damit ein 401 keine offene Session hinterlaesst.
    """
    owner_sub = resolve_owner_sub(request)
    db = SessionLocal()
    db.info["owner_sub"] = owner_sub
    try:
        yield db
    finally:
        db.close()


def system_db():
    """Tenant-freie Session fuer Startup-Migrationen/System-Seeds.

    owner_sub=None = System-Kontext: SELECT/UPDATE/DELETE laufen ungescoped,
    before_flush stempelt NICHT (neue Zeilen bleiben owner_sub=NULL = global,
    sofern nicht explizit gesetzt). NIE fuer Request-Handling verwenden.
    """
    db = SessionLocal()
    db.info["owner_sub"] = None
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
