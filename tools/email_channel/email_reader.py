# tools/email_channel/email_reader.py
"""
Lectura de respuestas de prospectos por IMAP (Agente 4 - Chat Manager).

Revisa la bandeja de entrada buscando correos NO leidos (respuestas de los
leads) y devuelve su remitente, asunto y cuerpo en texto plano. Marca cada
correo como leido para no procesarlo dos veces.

Si no hay credenciales IMAP configuradas, devuelve una lista vacia (el worker
de respuestas simplemente no encuentra nada que procesar).

imaplib es sincrono -> se ejecuta en un thread con asyncio.to_thread.
"""

import asyncio
import email as email_lib
import imaplib
import logging
from email.header import decode_header, make_header
from email.utils import parseaddr

from config.settings import get_settings
from database.db import session_scope
from database import crud

logger = logging.getLogger(__name__)

settings = get_settings()

MAX_FETCH = 25  # tope de correos por revision


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _extract_plain_body(msg) -> str:
    """Saca el texto plano del correo (ignora adjuntos y HTML pesado)."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"
                    )
                except Exception:  # noqa: BLE001
                    continue
        # Fallback: primer text/html convertido crudo
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"
                    )
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace"
        )
    except Exception:  # noqa: BLE001
        return msg.get_payload() or ""


def _fetch_sync() -> list[dict]:
    """Lectura bloqueante por IMAP. Corre dentro de asyncio.to_thread."""
    replies: list[dict] = []
    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        imap.login(settings.smtp_user, settings.smtp_password)
        imap.select("INBOX")
        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            return []
        ids = data[0].split()[:MAX_FETCH]
        for msg_id in ids:
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            
            raw_bytes = None
            for part in msg_data:
                if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray)):
                    raw_bytes = part[1]
                    break
            
            if not raw_bytes:
                continue

            msg = email_lib.message_from_bytes(raw_bytes)
            from_name, from_email = parseaddr(msg.get("From", ""))
            from_email_lower = (from_email or "").lower()
            
            is_valid_lead = False
            with session_scope() as db:
                if crud.get_lead_by_email(db, from_email_lower):
                    is_valid_lead = True
            
            if not is_valid_lead:
                logger.info("Omitiendo correo de remitente desconocido: %s", from_email_lower)
                continue

            replies.append({
                "from_email": from_email_lower,
                "from_name": _decode(from_name),
                "subject": _decode(msg.get("Subject")),
                "body": _extract_plain_body(msg).strip(),
                "message_id": msg.get("Message-ID", ""),
            })
            # Marcar como leido para no reprocesar.
            imap.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            imap.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return replies


async def fetch_unseen_replies() -> list[dict]:
    """Devuelve las respuestas nuevas (no leidas) de la bandeja.

    Returns:
        Lista de dicts {from_email, from_name, subject, body, message_id}.
        Vacia si no hay credenciales o no hay correos nuevos.
    """
    if not settings.imap_enabled:
        logger.debug("IMAP no configurado: no se revisan respuestas.")
        return []
    try:
        return await asyncio.to_thread(_fetch_sync)
    except (imaplib.IMAP4.error, OSError) as exc:
        logger.error("Fallo IMAP leyendo la bandeja: %s", exc)
        return []
