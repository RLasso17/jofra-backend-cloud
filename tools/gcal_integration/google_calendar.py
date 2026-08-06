# tools/calendar/google_calendar.py
"""
Integracion REAL con Google Calendar para el Agente 4 (Chat Manager & Closer).

Crea un evento en el calendario primario de Francisco, genera un link de
Google Meet (conferenceData + conferenceDataVersion=1) e invita al cliente.
Devuelve el hangoutLink para que el agente lo mande por WhatsApp.

Autenticacion OAuth2 (flujo de aplicacion instalada):
- credentials.json : se descarga de Google Cloud Console (OAuth client ID
  tipo "Desktop app" o "Web"). Ruta configurable en .env.
- token.json       : se crea/refresca AUTOMATICAMENTE tras el primer login.
  El primer login abre el navegador UNA vez; despues el refresh token
  renueva el acceso sin intervencion.

google-api-python-client es SINCRONO. Como el resto del sistema es asyncio
(FastAPI + agentes), las llamadas bloqueantes se ejecutan en un thread con
asyncio.to_thread() para no congelar el event loop.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Scope minimo para crear/leer eventos. Si se cambia, BORRAR token.json
# (el token viejo no tendra el nuevo permiso).
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Zona horaria del negocio (Jofra opera en Mexico).
JOFRA_TIMEZONE = "America/Monterrey"
TZINFO = ZoneInfo(JOFRA_TIMEZONE)

# Correo fijo de Francisco: SIEMPRE invitado a cada cita.
FRANCISCO_EMAIL = "francisco.cantu.jofra@gmail.com"

DEFAULT_MEETING_MINUTES = 30


class CalendarAuthError(Exception):
    """Falla de autenticacion/credenciales con Google Calendar."""


class CalendarBookingError(Exception):
    """Falla al crear el evento (fecha invalida, API, etc.)."""


# ======================================================================
# AUTENTICACION
# ======================================================================

def _credentials_path() -> str:
    path = settings.google_credentials_file
    return path if os.path.isabs(path) else str(BASE_DIR / path)


def _token_path() -> str:
    path = settings.google_token_file
    return path if os.path.isabs(path) else str(BASE_DIR / path)


def _is_headless() -> bool:
    """Detecta si el entorno carece de navegador interactivo (headless server/CI)."""
    if os.getenv("HEADLESS", "").lower() in ("1", "true", "yes"):
        return True
    if os.getenv("NO_BROWSER") or os.getenv("CI"):
        return True
    if not sys.stdin.isatty():
        return True
    if sys.platform != "win32" and not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        return True
    return False


def _load_or_refresh_credentials() -> Credentials:
    """Carga token.json; lo refresca si expiro; corre el flujo OAuth si no
    existe. Funcion SINCRONA (se llama dentro de asyncio.to_thread).
    """
    token_file = _token_path()
    creds: Credentials | None = None

    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except (ValueError, OSError) as exc:
            logger.warning("token.json ilegible (%s); se regenerara.", exc)
            creds = None

    if creds and creds.valid:
        return creds

    # Token expirado pero con refresh_token disponible -> refrescar.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist_token(creds)
            logger.info("Token de Google refrescado correctamente.")
            return creds
        except RefreshError as exc:
            logger.warning("Refresh del token fallo (%s); se requiere re-login.", exc)
            creds = None

    # Sin credenciales validas -> flujo OAuth interactivo (abre navegador).
    credentials_file = _credentials_path()
    if not os.path.exists(credentials_file):
        err_msg = (
            f"No existe {credentials_file}. Descarga el OAuth client ID "
            "(tipo Desktop/Web) de Google Cloud Console y guardalo ahi."
        )
        logger.error(err_msg)
        raise CalendarAuthError(err_msg)

    if _is_headless():
        err_msg = (
            "Google Calendar authentication required. Cannot run interactive browser "
            "flow in a headless environment without token.json."
        )
        logger.error(err_msg)
        raise CalendarAuthError(err_msg)

    try:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
        # run_local_server abre el navegador y levanta un listener temporal.
        creds = flow.run_local_server(port=0)
    except Exception as exc:  # noqa: BLE001 - el flujo puede fallar de varias formas
        err_msg = f"Flujo OAuth fallido: {exc}"
        logger.error(err_msg)
        raise CalendarAuthError(err_msg) from exc

    _persist_token(creds)
    logger.info("Autenticacion OAuth completada; token.json creado.")
    return creds


def _persist_token(creds: Credentials) -> None:
    with open(_token_path(), "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())


def _build_service():
    """Construye el cliente de Calendar (sincrono)."""
    creds = _load_or_refresh_credentials()
    # cache_discovery=False evita un warning ruidoso y problemas en entornos
    # sin permisos de escritura de cache.
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ======================================================================
# PARSEO DE FECHA/HORA
# ======================================================================

# Formatos que el Agente 4 puede extraer de la conversacion.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y")
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I%p", "%I %p")


def _parse_datetime(date_str: str, time_str: str) -> datetime:
    """Combina fecha + hora (varios formatos) en un datetime con tz Mexico.

    Acepta fecha ISO (2026-06-20) o local (20/06/2026) y hora 24h (16:00)
    o 12h (4:00 PM / 4PM). Lanza CalendarBookingError si no parsea.
    """
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip().upper().replace(".", "")
    # Normaliza variantes en espanol del meridiano.
    time_str = time_str.replace("A. M", "AM").replace("P. M", "PM")
    time_str = time_str.replace("A.M", "AM").replace("P.M", "PM")

    parsed_date = None
    for fmt in _DATE_FORMATS:
        try:
            parsed_date = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        raise CalendarBookingError(
            f"Fecha no reconocida: {date_str!r}. Usa AAAA-MM-DD o DD/MM/AAAA."
        )

    parsed_time = None
    for fmt in _TIME_FORMATS:
        try:
            parsed_time = datetime.strptime(time_str, fmt).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        raise CalendarBookingError(
            f"Hora no reconocida: {time_str!r}. Usa HH:MM (24h) o 'H:MM AM/PM'."
        )

    return datetime.combine(parsed_date, parsed_time, tzinfo=TZINFO)


# ======================================================================
# CREACION DEL EVENTO (TOOL PRINCIPAL DEL AGENTE 4)
# ======================================================================

def _create_event_sync(
    start_dt: datetime,
    duration_minutes: int,
    client_email: str | None,
    client_name: str,
) -> dict:
    """Llamada bloqueante a la API. Corre en thread via asyncio.to_thread."""
    service = _build_service()
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    attendees = [{"email": FRANCISCO_EMAIL, "organizer": True}]
    if client_email:
        attendees.append({"email": client_email})

    # request_id debe ser unico y estable por evento; el timestamp del inicio
    # sirve (no usamos Math.random/Date.now globales prohibidos en otros lados,
    # pero aqui es codigo normal de runtime).
    request_id = f"jofra-{int(start_dt.timestamp())}"

    event_body = {
        "summary": f"Jofra Solar x {client_name} — Diagnóstico de ahorro CFE",
        "description": (
            "Reunión de diagnóstico de ahorro en el recibo de CFE con paneles "
            "solares industriales.\n\n"
            "Jofra Sistemas y Equipos — Francisco Cantú.\n"
            "Tema: ahorro de hasta 98% en CFE, ROI 3–5 años, deducción 100% ISR "
            "primer año, servicio llave en mano.\n"
            f"Cliente: {client_name}."
        ),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": JOFRA_TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": JOFRA_TIMEZONE},
        "attendees": attendees,
        "conferenceData": {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ],
        },
    }

    event = (
        service.events()
        .insert(
            calendarId=settings.google_calendar_id,
            body=event_body,
            conferenceDataVersion=1,  # OBLIGATORIO para generar el link de Meet
            sendUpdates="all",         # envia la invitacion por correo
        )
        .execute()
    )
    return event


def _extract_meet_link(event: dict) -> str | None:
    """Saca el link de Meet del evento creado (varios lugares posibles)."""
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for entry in (event.get("conferenceData", {}) or {}).get("entryPoints", []):
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return None


async def schedule_meeting(
    date_str: str,
    time_str: str,
    client_email: str | None,
    client_name: str,
    duration_minutes: int = DEFAULT_MEETING_MINUTES,
) -> dict:
    """Agenda una reunion con Google Meet. Tool principal del Agente 4.

    Args:
        date_str: fecha ("2026-06-20" o "20/06/2026").
        time_str: hora ("16:00" o "4:00 PM").
        client_email: correo del cliente (puede ser None si solo se tiene
            su WhatsApp; en ese caso solo se invita a Francisco y el cliente
            recibe el link por chat).
        client_name: nombre del cliente/empresa para el titulo del evento.
        duration_minutes: duracion (default 30).

    Returns:
        dict con:
        - success (bool)
        - meet_link (str|None): el hangoutLink de Google Meet.
        - event_link (str|None): URL del evento en Calendar (htmlLink).
        - event_id (str|None)
        - start (str|None): inicio en ISO con tz.
        - error (str|None)
    """
    result = {
        "success": False,
        "meet_link": None,
        "event_link": None,
        "event_id": None,
        "start": None,
        "error": None,
    }

    # 1) Parsear fecha/hora ANTES de tocar la API.
    try:
        start_dt = _parse_datetime(date_str, time_str)
    except CalendarBookingError as exc:
        result["error"] = str(exc)
        logger.warning("schedule_meeting: %s", exc)
        return result

    if start_dt < datetime.now(tz=TZINFO):
        result["error"] = (
            f"La fecha/hora {start_dt.isoformat()} ya pasó. Sugiere al cliente "
            "un horario futuro."
        )
        logger.warning("schedule_meeting: %s", result["error"])
        return result

    # 2) Crear el evento (llamada bloqueante en thread).
    try:
        event = await asyncio.to_thread(
            _create_event_sync, start_dt, duration_minutes, client_email, client_name
        )
    except CalendarAuthError as exc:
        result["error"] = f"Error de autenticación con Google: {exc}"
        logger.error("schedule_meeting auth: %s", exc)
        return result
    except HttpError as exc:
        result["error"] = f"Error de la API de Calendar: {exc}"
        logger.error("schedule_meeting API: %s", exc)
        return result
    except Exception as exc:  # noqa: BLE001 - red u otros fallos del cliente
        result["error"] = f"Error inesperado al agendar: {exc}"
        logger.exception("schedule_meeting inesperado")
        return result

    meet_link = _extract_meet_link(event)
    result.update(
        success=True,
        meet_link=meet_link,
        event_link=event.get("htmlLink"),
        event_id=event.get("id"),
        start=start_dt.isoformat(),
    )
    if not meet_link:
        # El evento se creo pero Meet no se genero (raro: permisos de Meet en
        # la org, o cuenta sin Workspace). Se reporta sin marcar fallo total.
        logger.warning(
            "Evento %s creado pero sin link de Meet. Revisar permisos de "
            "Google Meet en la cuenta.", event.get("id"),
        )
    else:
        logger.info("Reunion agendada: %s | Meet: %s", start_dt.isoformat(), meet_link)

    return result


def check_auth_ready() -> dict:
    """Diagnostico sincrono (sin abrir navegador): ¿esta lista la auth?

    Util en el startup para avisar si falta credentials.json o el token.
    """
    creds_ok = os.path.exists(_credentials_path())
    token_ok = os.path.exists(_token_path())
    return {
        "credentials_file_present": creds_ok,
        "token_present": token_ok,
        "credentials_path": _credentials_path(),
        "token_path": _token_path(),
        "ready": creds_ok and token_ok,
    }
