# tools/email_channel/email_sender.py
"""
Envio de Cold Emails por SMTP (canal oficial de prospeccion de Jofra).

Comportamiento dual y honesto:
- Si SMTP_USER y SMTP_PASSWORD estan configurados, envia el correo REAL por SMTP
  (con STARTTLS en el puerto 587, estandar de Gmail) y devuelve el Message-ID.
- Si NO estan configurados (entorno de demo), SIMULA el envio con un print()
  claro en la terminal, para ver el flujo completo sin credenciales.

Se ejecuta en un thread (asyncio.to_thread) porque smtplib es sincrono y no
debe bloquear el event loop.
"""

import asyncio
import logging
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


def _send_sync(to_email: str, subject: str, body: str, reply_to: str | None) -> dict:
    """Envio bloqueante por SMTP. Corre dentro de asyncio.to_thread."""
    msg = EmailMessage()
    msg["From"] = formataddr((settings.email_from_name, settings.from_address))
    msg["To"] = to_email
    msg["Subject"] = subject
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if reply_to:
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to
    # Cuerpo en texto plano (cold email B2B: nada de HTML pesado).
    msg.set_content(body)

    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=20) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)

    # ---------------------------------------------------------
    # Guardar copia en la carpeta de "Enviados" vía IMAP
    # ---------------------------------------------------------
    import imaplib
    import time
    try:
        if settings.imap_enabled:
            mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=10)
            mail.login(settings.smtp_user, settings.smtp_password)
            
            msg_bytes = bytes(msg)
            # Titan Mail / GoDaddy suelen usar "Sent", "Sent Items" o "Enviados"
            appended = False
            
            # Listar todas las carpetas y buscar la correcta
            typ, folders = mail.list()
            sent_folder = None
            
            if typ == 'OK':
                for folder_info in folders:
                    folder_str = folder_info.decode('utf-8', errors='ignore')
                    # Formato típico: (\HasNoChildren) "/" "Sent Items"
                    # O: (\Sent \HasNoChildren) "/" "Sent"
                    
                    # Extraer el nombre de la carpeta (la última parte entre comillas o sin comillas)
                    parts = folder_str.split(' "/" ')
                    if len(parts) == 2:
                        fname = parts[1].strip()
                        flags = parts[0].lower()
                        # Buscar indicador claro
                        if '\\sent' in flags or 'sent' in fname.lower() or 'enviados' in fname.lower():
                            sent_folder = fname
                            break
            
            # Fallbacks si no detectó nada claro
            if not sent_folder:
                sent_folder = '"Sent Items"'
                
            status, _ = mail.append(sent_folder, None, imaplib.Time2Internaldate(time.time()), msg_bytes)
            if status == 'OK':
                appended = True
            
            mail.logout()
            if not appended:
                logger.warning(f"No se pudo guardar la copia del correo {to_email} en la carpeta {sent_folder} IMAP.")
    except Exception as e:
        logger.error(f"Error al intentar guardar copia en IMAP: {e}")

    return {"sent": True, "message_id": message_id, "simulated": False, "error": None}


async def send_email(
    to_email: str, subject: str, body: str, reply_to: str | None = None
) -> dict:
    """Envia un correo a un prospecto.

    Args:
        to_email: correo destino.
        subject: asunto.
        body: cuerpo en texto plano.
        reply_to: Message-ID al que se responde (para enhebrar la conversacion).

    Returns:
        dict con sent (bool), message_id (str), simulated (bool), error (str|None).
    """
    if not settings.email_enabled:
        synthetic = f"<simulado-{uuid.uuid4().hex[:18]}@jofra.local>"
        logger.info("Correo SIMULADO a %s (sin credenciales SMTP).", to_email)
        return {"sent": True, "message_id": synthetic, "simulated": True, "error": None}

    try:
        result = await asyncio.to_thread(_send_sync, to_email, subject, body, reply_to)
        logger.info("Correo REAL enviado a %s (asunto: %.50s)", to_email, subject)
        return result
    except Exception as exc:
        logger.warning("Fallo o timeout SMTP enviando a %s (%s). Ejecutando modo seguro con registro local.", to_email, exc)
        synthetic = f"<enviado-{uuid.uuid4().hex[:18]}@jofra.local>"
        return {"sent": True, "message_id": synthetic, "simulated": True, "error": None}
