import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func

from database.db import session_scope
from database.models import Lead, ChatMessage, OutboundQueueItem, MessageDirection, OutboundStatus, LeadStatus
from tools.email_channel.email_sender import _send_sync
from config.settings import get_settings
from llm.model_factory import coordinator_llm

logger = logging.getLogger(__name__)

def generate_weekly_metrics(db_session):
    """Obtiene las métricas de la última semana."""
    now = datetime.now(timezone.utc)
    one_week_ago = now - timedelta(days=7)

    # 1. Correos enviados (OutboundQueueItem en status SENT)
    sent_count = db_session.scalar(
        select(func.count()).select_from(OutboundQueueItem).filter(
            OutboundQueueItem.status == OutboundStatus.SENT,
            OutboundQueueItem.sent_at >= one_week_ago
        )
    ) or 0

    # 2. Respuestas recibidas (ChatMessage INBOUND)
    reply_count = db_session.scalar(
        select(func.count()).select_from(ChatMessage).filter(
            ChatMessage.direction == MessageDirection.INBOUND,
            ChatMessage.created_at >= one_week_ago
        )
    ) or 0

    # 3. Reuniones agendadas (Leads que llegaron a MEETING_SCHEDULED)
    meetings_count = db_session.scalar(
        select(func.count()).select_from(Lead).filter(
            Lead.status == LeadStatus.MEETING_SCHEDULED,
            Lead.updated_at >= one_week_ago
        )
    ) or 0
    
    # Detalle de reuniones agendadas
    meetings = db_session.scalars(
        select(Lead).filter(
            Lead.status == LeadStatus.MEETING_SCHEDULED,
            Lead.updated_at >= one_week_ago
        )
    ).all()
    
    meeting_details = [
        f"- {l.company_name or 'Empresa'} ({l.email}): Agendado recientemente." for l in meetings
    ]
    
    # Detalle de descartados esta semana (rechazos/48h)
    discarded_count = db_session.scalar(
        select(func.count()).select_from(Lead).filter(
            Lead.status == LeadStatus.DISCARDED,
            Lead.updated_at >= one_week_ago
        )
    ) or 0

    return {
        "sent": sent_count,
        "replies": reply_count,
        "meetings_count": meetings_count,
        "meeting_details": meeting_details,
        "discarded": discarded_count
    }


import litellm
from config.settings import get_settings

def analyze_metrics_with_llm(metrics) -> str:
    """Usa el LLM para generar un resumen ejecutivo del proyecto."""
    settings = get_settings()
    
    prompt = f"""
    Eres un analista de ventas para Jofra Sistemas y Equipos. Tu tarea es redactar un REPORTE SEMANAL de desempeño de la plataforma de agentes autónomos.
    
    Métricas de los últimos 7 días:
    - Correos enviados a prospectos: {metrics['sent']}
    - Respuestas recibidas: {metrics['replies']}
    - Reuniones (Google Meet) agendadas: {metrics['meetings_count']}
    - Leads descartados (por falta de respuesta o rechazo): {metrics['discarded']}
    
    Detalle de reuniones:
    {chr(10).join(metrics['meeting_details']) if metrics['meeting_details'] else 'Ninguna esta semana.'}
    
    Redacta un análisis ejecutivo (formato texto / email) que le enviaremos a la dirección.
    Analiza si el proyecto está funcionando bien, si hay un buen ratio de respuesta, qué significan los rechazos, etc. Sé profesional, analítico y motivador.
    (Omite saludos informales, solo entrega el cuerpo del reporte bien estructurado).
    """
    
    try:
        response = litellm.completion(
            model=f"ollama_chat/{settings.agent0_coordinator_model}",
            messages=[{"role": "user", "content": prompt}],
            api_base=settings.ollama_api_base,
            api_key=settings.ollama_api_key
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error llamando al LLM para el reporte: {e}")
        return f"Métricas crudas:\nEnviados: {metrics['sent']}\nRespuestas: {metrics['replies']}\nReuniones: {metrics['meetings_count']}\nDescartados: {metrics['discarded']}"


def send_weekly_report():
    """Ejecuta la generación y envío del reporte semanal."""
    logger.info("Iniciando generación de Reporte Semanal...")
    with session_scope() as db:
        metrics = generate_weekly_metrics(db)
        
    analysis = analyze_metrics_with_llm(metrics)
    
    # Construir el correo final
    subject = "Reporte Semanal de Desempeño - Agentes Autónomos Jofra"
    body = f"""Estimado equipo Jofra,

A continuación presentamos el reporte semanal de desempeño de la prospección automatizada:

{analysis}

Atentamente,
El Sistema de Agentes Autónomos
"""
    
    # Enviar correo a rlassoa17@gmail.com
    recipient = "rlassoa17@gmail.com"
    logger.info(f"Enviando reporte semanal a {recipient}")
    _send_sync(
        to_email=recipient,
        subject=subject,
        body=body,
        reply_to=None
    )
    logger.info("Reporte Semanal enviado exitosamente a rlassoa17@gmail.com.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    send_weekly_report()
