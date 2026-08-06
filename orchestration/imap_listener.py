import asyncio
import email
import imaplib
import logging
from email.header import decode_header
from typing import Optional

from config.settings import get_settings
from database.db import session_scope
from database.models import Lead
from orchestration.runners import handle_email_reply

logger = logging.getLogger(__name__)

def _get_text_from_email(msg: email.message.Message) -> str:
    text_content = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    text_content += part.get_payload(decode=True).decode()
                except Exception:
                    pass
    else:
        if msg.get_content_type() == "text/plain":
            try:
                text_content = msg.get_payload(decode=True).decode()
            except Exception:
                pass
    return text_content.strip()

def _extract_email_address(from_header: str) -> str:
    # Extracts email from "Name <email@domain.com>" or "email@domain.com"
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].split(">")[0].strip().lower()
    return from_header.strip().lower()

async def run_imap_listener_cycle():
    settings = get_settings()
    if not settings.imap_enabled:
        logger.warning("IMAP no está configurado o habilitado en .env")
        return

    logger.info("Conectando a IMAP: %s", settings.imap_host)
    try:
        # Usar asyncio.to_thread porque imaplib es síncrono
        mail = await asyncio.to_thread(imaplib.IMAP4_SSL, settings.imap_host, settings.imap_port)
        await asyncio.to_thread(mail.login, settings.smtp_user, settings.smtp_password)
        await asyncio.to_thread(mail.select, "INBOX")

        # Buscar correos no leídos
        status, messages = await asyncio.to_thread(mail.search, None, '(UNSEEN)')
        if status != "OK":
            logger.error("Error buscando correos UNSEEN")
            return

        message_ids = messages[0].split()
        if not message_ids:
            logger.info("No hay correos nuevos.")
            await asyncio.to_thread(mail.logout)
            return

        logger.info(f"Encontrados {len(message_ids)} correos nuevos. Procesando...")

        for msg_id in message_ids:
            # Obtener el contenido del correo
            res, msg_data = await asyncio.to_thread(mail.fetch, msg_id, "(RFC822)")
            if res != "OK":
                continue

            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # Decodificar Subject
                    subject_header = msg.get("Subject", "")
                    decoded_list = decode_header(subject_header)
                    subject = ""
                    for text, charset in decoded_list:
                        if isinstance(text, bytes):
                            subject += text.decode(charset or 'utf-8', errors='ignore')
                        else:
                            subject += text

                    # Obtener remitente
                    from_header = msg.get("From", "")
                    sender_email = _extract_email_address(from_header)
                    
                    # Obtener texto del cuerpo
                    body_text = _get_text_from_email(msg)
                    message_id_header = msg.get("Message-ID", "")

                    logger.info(f"Correo recibido de: {sender_email} | Asunto: {subject}")

                    # Verificar si el remitente existe en nuestra BD de Leads
                    with session_scope() as db:
                        lead = db.query(Lead).filter(Lead.email == sender_email).first()
                        lead_id = lead.id if lead else None

                    if lead_id:
                        logger.info(f"Lead {lead_id} encontrado para el correo {sender_email}. Enviando al Agente 4...")
                        # Enviar a procesar asincrónicamente
                        # No bloqueamos el loop principal
                        asyncio.create_task(
                            handle_email_reply(
                                lead_id=lead_id, 
                                incoming_text=body_text, 
                                subject_in=subject,
                                in_reply_to=message_id_header
                            )
                        )
                    else:
                        logger.info(f"Correo de {sender_email} ignorado (No es un Lead registrado).")
                    
        # Marcar todos los procesados como vistos, cerrar conexión
        await asyncio.to_thread(mail.logout)

    except Exception as e:
        logger.exception(f"Error crítico en IMAP Listener: {e}")

async def start_imap_listener_loop(interval_seconds=60):
    """Bucle infinito para correr como proceso en segundo plano"""
    logger.info("Iniciando IMAP Listener Daemon...")
    while True:
        await run_imap_listener_cycle()
        await asyncio.sleep(interval_seconds)

if __name__ == "__main__":
    asyncio.run(run_imap_listener_cycle())
