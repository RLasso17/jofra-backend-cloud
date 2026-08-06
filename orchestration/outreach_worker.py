import sys
import os
from pathlib import Path

# Garantizar que el directorio raíz de la aplicación esté en sys.path al inicio absoluto
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

# orchestration/outreach_worker.py
"""
Worker asincrono de la cola de outreach (Agente 3).

Flujo (anti-ban de WhatsApp):
1. Cuando un lead pasa a ready_for_outreach, schedule_lead_outreach() crea un
   item en la cola con un retraso humano aleatorio de 1 a 5 minutos
   (asyncio.sleep) y lanza una tarea que espera ese tiempo.
2. Tras la espera, SI is_human_agent_assigned sigue en False, invoca al
   Agente 3 para REDACTAR el mensaje (no antes: si el humano interviene o el
   lead responde durante la espera, no gastamos tokens), lo envia por WhatsApp
   y lo registra como mensaje del bot.
3. Si durante la espera un humano tomo la conversacion, el item se cancela y
   el bot se calla.

Robustez ante reinicios: al arrancar, el worker reprograma todos los items
PENDING con su tiempo restante (scheduled_at - ahora), asi el retraso
sobrevive a un reinicio del proceso.
"""

import asyncio
import logging
from datetime import datetime, timezone

from config.settings import get_settings
from database import crud
from database.db import session_scope
from orchestration.runners import generate_outreach_email, send_outreach_email

logger = logging.getLogger(__name__)

settings = get_settings()

# Referencias fuertes a las tareas en vuelo (evita que el GC las cancele).
_pending_tasks: set[asyncio.Task] = set()

# Semáforo global anti-avalancha: limita cuántos cold emails se generan/envían
# en paralelo (protege al LLM de Ollama de saturarse en envíos masivos).
_sem = asyncio.Semaphore(settings.outreach_concurrency)


def _track(task: asyncio.Task) -> None:
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


def _remaining_seconds(scheduled_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    # scheduled_at puede venir naive desde SQLite; asumir UTC.
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    return max(0.0, (scheduled_at - now).total_seconds())


async def _process_after_delay(item_id: int, delay_seconds: float) -> None:
    """Espera el retraso humano y, si procede, genera y envia el outreach.

    El envío/generación pasa por un semáforo global: aunque haya cientos de leads
    encolados a la vez, solo N se procesan en paralelo (anti-avalancha).
    """
    logger.info("Outreach item %s: esperando %.0fs (retraso humano).", item_id, delay_seconds)
    await asyncio.sleep(delay_seconds)
    async with _sem:
        await _generate_and_send(item_id)


async def _generate_and_send(item_id: int) -> None:
    from config.settings import is_system_active
    from database.models import OutboundQueueItem, OutboundStatus
    
    # 1) Cargar item y lead; debe tener correo.
    with session_scope() as db:
        item = db.get(OutboundQueueItem, item_id)
        if item is None:
            return
        if item.status not in (OutboundStatus.PENDING, OutboundStatus.FAILED):
            return
        item.status = OutboundStatus.PENDING
        lead = crud.get_lead_by_id(db, item.lead_id)
        if lead is None:
            crud.mark_outbound_failed(db, item, "Lead inexistente.")
            return
        if not lead.email:
            crud.mark_outbound_failed(db, item, "Lead sin correo: borrando de BD.")
            db.delete(lead)
            db.delete(item)
            db.commit()
            return
        lead_id = lead.id
        has_draft = bool(lead.draft_subject and lead.draft_body)

    # 2) Generar el cold email (asunto + cuerpo) si no existe borrador
    if not has_draft:
        try:
            email = await generate_outreach_email(lead_id)
            if not email or not email.get("body"):
                with session_scope() as db:
                    item = db.get(OutboundQueueItem, item_id)
                    if item:
                        crud.mark_outbound_failed(db, item, "Agente 3 devolvió vacío.")
                return
                
            # Guardar en BD para que el usuario pueda verlo/editarlo en el Hub
            with session_scope() as db:
                lead = crud.get_lead_by_id(db, lead_id)
                item = db.get(OutboundQueueItem, item_id)
                if lead and item:
                    crud.set_draft_email(db, lead, email["subject"], email["body"])
                    crud.set_outbound_body(db, item, f"ASUNTO: {email['subject']}\n\n{email['body']}")
        except Exception:
            logger.exception("Agente 3 falló generando el cold email del lead %s.", lead_id)
            with session_scope() as db:
                item = db.get(OutboundQueueItem, item_id)
                if item:
                    crud.mark_outbound_failed(db, item, "Fallo de generación del Agente 3.")
            return

    # 3) Verificar si el motor global está activo ANTES de mandar
    if not is_system_active():
        # Actualizar scheduled_at en la BD para evitar reencolar infinitamente en el bucle de 10s
        with session_scope() as db:
            item = db.get(OutboundQueueItem, item_id)
            if item:
                item.scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=60)
                db.commit()
        return

    # 4) Cargar el borrador FINAL (puede haber sido editado manualmente) y enviar
    with session_scope() as db:
        item = db.get(OutboundQueueItem, item_id)
        if item is None or item.status != OutboundStatus.PENDING:
            return
        lead = crud.get_lead_by_id(db, lead_id)
        if lead is None or not lead.draft_subject or not lead.draft_body:
            crud.mark_outbound_failed(db, item, "Borrador incompleto.")
            return
        if lead.email_sent:
            crud.mark_outbound_failed(db, item, "Correo ya fue enviado previamente.")
            return
        subject = lead.draft_subject
        body = lead.draft_body
        crud.set_outbound_body(db, item, f"ASUNTO: {subject}\n\n{body}")

    result = await send_outreach_email(lead_id, subject, body)

    # 5) Marcar el item segun resultado.
    with session_scope() as db:
        item = db.get(OutboundQueueItem, item_id)
        if item is None:
            return
        if result.get("sent"):
            crud.mark_outbound_sent(db, item, result.get("message_id") or "")
        else:
            crud.mark_outbound_failed(db, item, result.get("error") or "Envío fallido.")


def schedule_lead_outreach(lead_id: int) -> int | None:
    """Encola un lead para outreach y lanza la tarea con su retraso humano.

    Lo llama el flujo cuando el Agente 2 aprueba un lead (ready_for_outreach).
    Devuelve el id del item de cola creado.
    """
    from datetime import timedelta
    with session_scope() as db:
        # Verificar si ya tiene un item PENDING para evitar duplicados
        from database.models import OutboundQueueItem, OutboundStatus
        from sqlalchemy import select
        existing = db.scalar(
            select(OutboundQueueItem).where(
                OutboundQueueItem.lead_id == lead_id,
                OutboundQueueItem.status == OutboundStatus.PENDING
            )
        )
        if existing:
            return existing.id

        item = crud.enqueue_lead_for_outreach(
            db,
            lead_id=lead_id,
            delay_min_seconds=settings.outreach_delay_min_seconds,
            delay_max_seconds=settings.outreach_delay_max_seconds,
        )
        item_id = item.id
        delay = _remaining_seconds(item.scheduled_at)

    # NO lanzamos asyncio.create_task() aqui. Dejamos que run_outreach_worker
    # lo recoja cuando su delay se cumpla, evitando saturar asyncio con miles de tareas.
    return item_id


def schedule_all_ready_outreach() -> int:
    """BULK: encola el cold email de TODOS los leads en ready_for_outreach que
    aún no han sido contactados. Cada uno lleva su retraso humano y pasa por el
    semáforo, así que una avalancha de leads no satura el LLM ni el SMTP.

    Devuelve cuántos leads se encolaron.
    """
    with session_scope() as db:
        leads = crud.get_leads_ready_for_outreach(db)
        lead_ids = [lead.id for lead in leads]
    for lead_id in lead_ids:
        schedule_lead_outreach(lead_id)
    if lead_ids:
        logger.info("Outreach BULK: %s leads encolados para envío.", len(lead_ids))
    return len(lead_ids)


async def reschedule_pending_on_startup() -> int:
    """Reprograma los items PENDING tras un reinicio (con su tiempo restante).

    Devuelve cuantos items se reprogramaron.
    """
    # Ya no lanzamos tareas de asyncio masivas al inicio.
    # El bucle de run_outreach_worker los ira procesando naturalmente
    # cuando scheduled_at <= now.
    with session_scope() as db:
        pending = crud.get_pending_outbound_items(db)
        return len(pending)

async def run_outreach_worker(stop_event: asyncio.Event) -> None:
    """Worker en segundo plano que procesa la cola de outbound_queue_items cada 10s."""
    logger.info("Worker de cola de Outreach iniciado (bucle continuo 24/7).")
    while not stop_event.is_set():
        try:
            with session_scope() as db:
                due_items = crud.get_due_outbound_items(db)
                item_ids = [it.id for it in due_items]

            for item_id in item_ids:
                if stop_event.is_set():
                    break
                async with _sem:
                    await _generate_and_send(item_id)
        except Exception as exc:
            logger.exception("Error en bucle de outreach worker: %s", exc)

        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
