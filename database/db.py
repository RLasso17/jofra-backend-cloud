# database/db.py
"""
Conexion y ciclo de vida de la base de datos (SQLite por defecto).

- WAL activado: permite que el webhook de FastAPI y el runner de agentes
  lean/escriban concurrentemente sin "database is locked".
- foreign_keys=ON: SQLite no aplica FKs por defecto; aqui se fuerza.
- busy_timeout: espera hasta 5s ante un lock en vez de fallar al instante.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

_is_sqlite = settings.effective_database_url.startswith("sqlite")

engine = create_engine(
    settings.effective_database_url,
    # check_same_thread=False: FastAPI ejecuta background tasks en threadpool;
    # cada sesion sigue siendo de un solo hilo, pero la conexion puede migrar.
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base declarativa de todos los modelos del proyecto."""


# Flag de inicialización perezosa: garantiza que las tablas existan sin importar
# el punto de entrada (main.py, `adk web`, workers, scripts) — no solo el lifespan.
_initialized = False


def init_db() -> None:
    """Crea las tablas si no existen. Idempotente; seguro llamarlo varias veces."""
    global _initialized
    # Importar los modelos registra las tablas en Base.metadata.
    from database import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()
    _initialized = True
    logger.info("Base de datos inicializada en %s", settings.effective_database_url)


def _ensure_initialized() -> None:
    """Inicializa la DB la PRIMERA vez que se abre una sesión, sin importar quién
    la abra. Esto cubre `adk web` (que NO ejecuta el lifespan de main.py) y evita
    el error 'no such table: leads'."""
    if not _initialized:
        try:
            init_db()
        except Exception:  # noqa: BLE001 - no bloquear si ya está creada por otro proceso
            logger.exception("Fallo en la inicialización perezosa de la DB.")


# Migraciones ligeras: columnas agregadas DESPUES de que existiera una jofra.db.
# create_all() NO altera tablas existentes, asi que aqui se agregan columnas
# faltantes con ALTER TABLE (idempotente y seguro: solo agrega si no existe).
# Formato: (tabla, columna, definicion_sql_con_default).
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # Migracion al canal de Cold Email (pivote estrategico):
    ("leads", "email", "VARCHAR"),
    ("leads", "email_sent", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("leads", "has_replied", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("leads", "meeting_scheduled", "BOOLEAN DEFAULT 0 NOT NULL"),
]


def _run_lightweight_migrations() -> None:
    """Agrega columnas nuevas a tablas ya existentes (solo SQLite)."""
    if not _is_sqlite:
        return  # en otros motores, usar una herramienta de migracion formal

    from sqlalchemy import text

    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            existing = {
                row[1]  # row = (cid, name, type, notnull, dflt_value, pk)
                for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            if not existing:
                continue  # la tabla aun no existe (create_all la hara con la columna)
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                logger.info(
                    "Migracion: columna %s.%s agregada a la base de datos existente.",
                    table, column,
                )


def get_db() -> Generator[Session, None, None]:
    """Dependency de FastAPI: una sesion por request, cerrada al final."""
    _ensure_initialized()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager transaccional para uso FUERA de FastAPI
    (background tasks, runner de agentes, scripts).

    Commit al salir limpio; rollback ante cualquier excepcion.
    """
    _ensure_initialized()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
