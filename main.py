import sys
import os
from pathlib import Path

# Garantizar que el directorio raíz de la aplicación esté en sys.path al inicio absoluto
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# main.py
"""
Entry point del motor de prospección B2B de Jofra Sistemas y Equipos.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config.settings import get_settings
settings = get_settings()

from database.db import init_db
from orchestration.email_reply_worker import poll_once, run_email_reply_worker
from orchestration.outreach_worker import (
    reschedule_pending_on_startup,
    schedule_all_ready_outreach,
    schedule_lead_outreach,
    run_outreach_worker,
)
from database.db import session_scope
from orchestration.outreach_worker import (
    reschedule_pending_on_startup,
    schedule_all_ready_outreach,
    schedule_lead_outreach,
    run_outreach_worker,
)
from database.db import session_scope

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("jofra")

settings = get_settings()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from orchestration.weekly_report import send_weekly_report

_stop_event: asyncio.Event | None = None
_reply_task: asyncio.Task | None = None
_outreach_task: asyncio.Task | None = None
async def scheduled_daily_extraction():
    logger.info("Ejecutando extracción diaria programada de 150 leads (08:00 AM CDMX)...")
    try:
        from scripts.run_daily_extraction import run_daily_extraction
        await run_daily_extraction(150)
    except Exception as exc:
        logger.error("Error en extracción diaria programada: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ----------------------------- startup -----------------------------
    global _stop_event, _reply_task, _outreach_task, _scheduler
    init_db()

    # Garantizar que el rechazo de Juan Maldonado persista
    try:
        from sqlalchemy import text
        with session_scope() as db:
            db.execute(text("UPDATE leads SET status = 'discarded' WHERE email = 'juan.maldonado@litopos.com' OR company_name LIKE '%Marroca%'"))
            db.commit()
    except Exception:
        pass

    try:
        await reschedule_pending_on_startup()
        schedule_all_ready_outreach()
    except Exception as exc:
        logger.error("Error en startup scheduling: %s", exc)

    # Arranca los workers de segundo plano (Outreach continuo e IMAP de respuestas).
    _stop_event = asyncio.Event()
    try:
        _reply_task = asyncio.create_task(run_email_reply_worker(_stop_event))
    except Exception as exc:
        logger.error("Error arrancando reply worker: %s", exc)

    try:
        _outreach_task = asyncio.create_task(run_outreach_worker(_stop_event))
    except Exception as exc:
        logger.error("Error arrancando outreach worker: %s", exc)

    # Iniciar el scheduler para tareas periódicas (Extracción diaria a las 8:00 AM y Reporte Semanal los LUNES a las 08:00 AM CDMX)
    try:
        _scheduler = AsyncIOScheduler()
        _scheduler.add_job(
            send_weekly_report,
            trigger='cron',
            day_of_week='mon',
            hour=8,
            minute=0,
            timezone='America/Mexico_City'
        )
        # Búsqueda e inyección automática diaria de 150 leads nuevos a las 08:00 AM (CDMX)
        _scheduler.add_job(
            scheduled_daily_extraction,
            trigger='cron',
            hour=8,
            minute=0,
            timezone='America/Mexico_City'
        )
        _scheduler.start()
    except Exception as exc:
        logger.error("Error arrancando scheduler: %s", exc)

    # Verificación Catch-Up de arranque: Si el bot estuvo apagado a las 8:00 AM, completar cuota de 150 leads y envíos hoy mismo
    try:
        from datetime import datetime, timezone
        from database.models import Lead
        from sqlalchemy import select, func
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with session_scope() as db:
            leads_today = db.scalar(select(func.count(Lead.id)).where(Lead.created_at >= today_start)) or 0
        if leads_today < 150:
            logger.info("Catch-up detectado en arranque (leads hoy: %d < 150). Iniciando extracción automática...", leads_today)
            asyncio.create_task(scheduled_daily_extraction())
    except Exception as exc:
        logger.error("Error en catch-up de arranque: %s", exc)

    logger.info("Motor de prospección Jofra iniciado (canal: COLD EMAIL).")
    if not settings.email_enabled:
        logger.warning(
            "SMTP no configurado (SMTP_USER/SMTP_PASSWORD): los envíos de correo "
            "se SIMULAN con print() en la terminal."
        )
    if not settings.imap_enabled:
        logger.warning("IMAP no configurado: no se leerán respuestas de la bandeja.")
    yield
    # ---------------------------- shutdown -----------------------------
    if _stop_event:
        _stop_event.set()
    if _scheduler:
        _scheduler.shutdown()
    if _reply_task:
        try:
            await asyncio.wait_for(_reply_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _reply_task.cancel()
    logger.info("Motor de prospección Jofra detenido.")


app = FastAPI(
    title="""
Jofra AI - Motor de Prospección B2B & Hub Dashboard
Versión: 2.5 (FastAPI + SQLite + LiteLLM + React/Alpine Hub)
# Railway Trigger: 2026-08-05-v1.0.1
""",
    description=(
        "Ecosistema multi-agente (google-adk) para prospección automatizada de "
        "clientes industriales de paneles solares en México, vía Cold Email."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

from api.hub import router as hub_router
app.include_router(hub_router)

@app.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok", "service": "jofra-prospecting-engine", "channel": "cold_email"}


@app.post("/admin/outreach/{lead_id}", tags=["ops"])
async def trigger_outreach(lead_id: int) -> dict:
    """Encola un lead para enviarle el cold email (lo dispara el Agente 2 al
    aprobarlo, o manualmente para pruebas)."""
    item_id = schedule_lead_outreach(lead_id)
    return {"status": "queued", "lead_id": lead_id, "outbound_item_id": item_id}


@app.post("/admin/outreach-all", tags=["ops"])
async def trigger_outreach_all() -> dict:
    """BULK: encola el cold email de TODOS los leads ready_for_outreach que aún
    no fueron contactados (con retraso humano y concurrencia acotada)."""
    n = schedule_all_ready_outreach()
    return {"status": "queued", "leads_encolados": n}


@app.post("/admin/trigger-daily-extraction", tags=["ops"])
async def trigger_daily_extraction() -> dict:
    """Dispara la búsqueda e inyección incremental diaria de 150 leads nuevos."""
    try:
        from scripts.run_daily_extraction import run_daily_extraction
        asyncio.create_task(run_daily_extraction(150))
        return {"status": "started", "message": "Extracción diaria incremental iniciada directamente en segundo plano."}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.post("/admin/poll-emails", tags=["ops"])
async def poll_emails() -> dict:
    """Revisa la bandeja de entrada AHORA (sin esperar el ciclo automático)."""
    n = await poll_once()
    return {"status": "ok", "replies_processed": n}





if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
