import sys
import os
from pathlib import Path

# Garantizar que el directorio raíz de la aplicación esté en sys.path al inicio absoluto
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

# orchestration/email_reply_worker.py
"""
Worker que LEE la bandeja de entrada (IMAP) buscando respuestas de los leads.
"""

import asyncio
import logging
from sqlalchemy import select

from config.settings import get_settings
from database import crud
from database.db import session_scope
from database.models import ChatMessage, LeadStatus, MessageDirection, SenderType
from orchestration.runners import handle_email_reply
from tools.email_channel import email_reader
from tools.email_channel.email_sender import send_email

logger = logging.getLogger(__name__)

settings = get_settings()

POLL_INTERVAL_SECONDS = 3600  # revisar la bandeja cada hora (ahorro de tokens)

# Semáforo anti-avalancha: aunque lleguen decenas de respuestas a la vez, solo
# N se procesan (LLM + envío) en paralelo, sin saturar Ollama ni el SMTP.
_sem = asyncio.Semaphore(settings.reply_concurrency)


def classify_reply_sentiment(text: str) -> str:
    """Helper de clasificación de sentimiento para respuestas de correo entrantes.

    Devuelve:
        "REJECTED" | "INTERESTED" | "REFERRAL" | "AUTO_REPLY"
    """
    if not text:
        return "INTERESTED"

    t = text.lower()

    # 1. Indicadores de Respuesta Automática / Fuera de la oficina
    auto_reply_phrases = [
        "fuera de la oficina",
        "out of office",
        "respuesta automática",
        "respuesta automatica",
        "auto-reply",
        "autoreply",
        "vacaciones",
        "ausente de la oficina",
    ]
    if any(phrase in t for phrase in auto_reply_phrases):
        return "AUTO_REPLY"

    # 2. Solicitudes de reunión y frases de interés directo (evaluadas ANTES de rechazo)
    meeting_phrases = [
        "agendar",
        "agendemos",
        "reunión",
        "reunion",
        "cita",
        "llamada",
        "videollamada",
        "demostración",
        "demostracion",
        "demo",
        "me interesa agendar",
        "quiero agendar",
        "nos interesa agendar",
        "programar reunión",
        "programar reunion",
        "programar cita",
        "coordinar una reunión",
        "coordinar una reunion",
        "coordinar una llamada",
    ]
    if any(phrase in t for phrase in meeting_phrases):
        return "INTERESTED"

    # 3. Palabras clave de redirección / referral positivo (evaluadas ANTES de rechazo)
    referral_phrases = [
        "contacta a",
        "comunícate con",
        "comunicate con",
        "escribe a",
        "mi compañero",
        "mi companero",
        "habla con",
        "dirígete a",
        "dirigete a",
        "puedes contactar a",
        "puedes hablar con",
        "te recomiendo hablar con",
        "te recomiendo contactar a",
        "remítete a",
        "remitete a",
    ]
    if any(phrase in t for phrase in referral_phrases):
        return "REFERRAL"

    # 4. Palabras clave de rechazo / opt-out
    rejection_phrases = [
        "no nos interesa",
        "no me interesa",
        "favor de removerme",
        "remover de la lista",
        "removerme de",
        "lista de distribucion",
        "lista de distribución",
        "ya tenemos paneles",
        "ya contamos con",
        "contamos con paneles",
        "disponemos de paneles",
        "no interesa",
        "dejar de escribir",
        "desuscribir",
        "cancelar suscripción",
        "cancelar suscripcion",
        "no escribir más",
        "no escribir mas",
        "removerme",
        "no estamos interesados",
        "no estoy interesado",
        "no estar interesado",
    ]
    if any(phrase in t for phrase in rejection_phrases):
        return "REJECTED"

    # 5. Por defecto: Interés positivo
    return "INTERESTED"


async def _process_reply(reply: dict) -> None:
    from_email = reply.get("from_email", "")
    body = reply.get("body", "")
    subject = reply.get("subject", "")
    msg_id = reply.get("message_id", "")
    if not from_email or not body:
        return

    with session_scope() as db:
        lead = crud.get_lead_by_email(db, from_email)
        if lead is None:
            logger.info("Respuesta de %s sin lead asociado; ignorada.", from_email)
            return
        lead_id = lead.id

        # Verificación de mensaje duplicado (Idempotencia - TC-R3-CC04)
        if msg_id:
            existing_msg = db.scalars(
                select(ChatMessage).where(ChatMessage.email_message_id == msg_id)
            ).first()
            if existing_msg:
                logger.info("Mensaje %s ya fue procesado anteriormente; ignorando duplicado.", msg_id)
                return

        # Verificación de intervención humana (Modo Híbrido - TC-R3-CC05)
        if not crud.is_bot_allowed_for_lead(db, lead_id):
            crud.mark_has_replied(db, lead)
            crud.save_chat_message(
                db, lead_id=lead_id, direction=MessageDirection.INBOUND,
                sender_type=SenderType.LEAD, body=body,
                email_message_id=msg_id or None, message_type="email",
            )
            logger.info("Lead %s asignado a humano; bot silenciado.", lead_id)
            return

        crud.mark_has_replied(db, lead)
        crud.save_chat_message(
            db, lead_id=lead_id, direction=MessageDirection.INBOUND,
            sender_type=SenderType.LEAD, body=body,
            email_message_id=msg_id or None, message_type="email",
        )
        contact_name = lead.contact_name

    # Evaluar sentimiento del correo entrante
    text_to_classify = f"{subject} {body}" if subject else body
    sentiment = classify_reply_sentiment(text_to_classify)
    logger.info("Respuesta de lead %s clasificada como: %s", lead_id, sentiment)

    if sentiment == "AUTO_REPLY":
        logger.info("Respuesta automática / fuera de oficina recibida para lead %s; ignorando.", lead_id)
        return

    if sentiment == "REJECTED":
        logger.info("Lead %s expresó rechazo / opt-out. Cancelando outreach y enviando correo de cierre.", lead_id)
        with session_scope() as db:
            lead = crud.get_lead_by_id(db, lead_id)
            if lead:
                try:
                    crud.transition_lead_status(
                        db, lead, LeadStatus.DISCARDED,
                        reason="Opt-out / no interesado", actor="email_reply_worker",
                        force=True,
                    )
                except crud.InvalidStateTransition:
                    pass
                crud.cancel_pending_outbound(db, lead_id, reason="Opt-out rejection received")

        # Enviar correo de cierre cortés
        greeting_name = f" {contact_name}" if contact_name else ""
        closing_body = f"Entendido{greeting_name}. Muchas gracias por su tiempo y quedamos a sus órdenes."
        closing_subject = f"Re: {subject}" if subject else "Re: Propuesta Jofra"

        result = await send_email(from_email, closing_subject, closing_body, reply_to=msg_id or None)

        with session_scope() as db:
            crud.save_chat_message(
                db, lead_id=lead_id, direction=MessageDirection.OUTBOUND,
                sender_type=SenderType.BOT, body=f"[Asunto: {closing_subject}]\n{closing_body}",
                email_message_id=result.get("message_id"), message_type="email",
            )
        return

    # Si el sentimiento es INTERESTED o REFERRAL:
    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        if lead and lead.status in (LeadStatus.READY_FOR_OUTREACH, LeadStatus.NEW, LeadStatus.QUALIFYING):
            try:
                crud.transition_lead_status(
                    db, lead, LeadStatus.IN_CONVERSATION,
                    reason="El prospecto respondió el correo", actor="email_reply_worker",
                    force=True,
                )
            except crud.InvalidStateTransition:
                pass

    logger.info("Lead %s respondió por correo (sentiment=%s); despachando al Agente 4.", lead_id, sentiment)
    await handle_email_reply(lead_id, body, subject_in=subject, in_reply_to=msg_id)


async def _guarded_process(reply: dict) -> None:
    """Procesa una respuesta bajo el semáforo (concurrencia acotada con retry)."""
    async with _sem:
        for attempt in range(3):
            try:
                await _process_reply(reply)
                break
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))
                else:
                    logger.exception("Error procesando respuesta de %s", reply.get("from_email"))
                    break


async def poll_once() -> int:
    """Revisa la bandeja y procesa las respuestas EN PARALELO (acotado por el
    semáforo). Maneja avalanchas: muchas respuestas a la vez, sin saturar el LLM.
    """
    from config.settings import is_system_active
    if not is_system_active():
        return 0

    replies = await email_reader.fetch_unseen_replies()
    batch_size = get_settings().reply_batch_size
    replies = replies[:batch_size]
    if replies:
        await asyncio.gather(*[_guarded_process(r) for r in replies])
        
    # Limpiar leads que no han respondido en 48 horas
    with session_scope() as db:
        crud.discard_unresponsive_leads(db, hours=48)
        
    return len(replies)


async def run_email_reply_worker(stop_event: asyncio.Event) -> None:
    """Bucle del worker: revisa la bandeja cada POLL_INTERVAL hasta que se pida parar."""
    if not settings.imap_enabled:
        logger.warning(
            "IMAP no configurado (SMTP_USER/SMTP_PASSWORD/IMAP_HOST): el lector de "
            "respuestas está inactivo. Configúralo en .env para leer correos."
        )
    logger.info("Worker de respuestas por correo iniciado (cada %ss).", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            n = await poll_once()
            if n:
                logger.info("Procesadas %s respuestas de correo.", n)
        except Exception:
            logger.exception("Error en el ciclo del lector de correos.")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("Worker de respuestas por correo detenido.")
