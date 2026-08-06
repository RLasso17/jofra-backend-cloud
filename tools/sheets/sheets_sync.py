# tools/sheets/sheets_sync.py
"""
Sincronización EN VIVO del pipeline de leads con un Google Sheet compartido.

- Cada vez que un lead se crea o cambia (correo, estado, contestó, agendó), se
  hace upsert de su fila en el Sheet (visible en tiempo real).
- DEDUP: mantiene en memoria el conjunto de correos ya presentes en el Sheet;
  `email_in_sheet()` permite la validación "cero duplicados" sin leer todo el
  Sheet en cada consulta.

Autenticación por SERVICE ACCOUNT (ideal para un servidor headless): comparte el
Sheet con el email del service account. Si no está configurado, TODO es no-op
(el sistema sigue funcionando solo con la base de datos).

La API de Sheets es síncrona -> se ejecuta en threads con asyncio.to_thread.
"""

import asyncio
import logging
import os

from config.settings import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Columnas del Sheet (orden fijo). 'Correo' es la llave de dedup/upsert.
HEADERS = ["ID", "Empresa", "Contacto", "Puesto", "Sector", "Ciudad", "Correo",
           "Contexto Icebreaker", "Probabilidad", "Estado", "Correo enviado", "Contestó", "Reunión agendada",
           "Actualizado"]
_EMAIL_COL = HEADERS.index("Correo")  # 0-based

_service = None
_email_rows: dict[str, int] | None = None   # email -> número de fila (1-based)
_lock = asyncio.Lock()


def _creds_path() -> str:
    p = settings.google_sheets_credentials_file
    return p if os.path.isabs(p) else str(BASE_DIR / p)


def _get_service():
    global _service
    if _service is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            _creds_path(), scopes=_SCOPES
        )
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service


def _tab() -> str:
    return settings.google_sheet_tab


def _lead_to_row(lead) -> list:
    def si_no(v):
        return "Sí" if v else "No"
    return [
        lead.id,
        lead.company_name or "",
        lead.contact_name or "",
        lead.contact_role or "",
        lead.sector or "",
        lead.city or "",
        (lead.email or "").lower(),
        lead.icebreaker_context or "",
        lead.purchase_probability or "",
        getattr(lead.status, "value", "") or "",
        si_no(lead.email_sent),
        si_no(lead.has_replied),
        si_no(lead.meeting_scheduled),
        lead.updated_at.strftime("%Y-%m-%d %H:%M") if getattr(lead, "updated_at", None) else "",
    ]


def _ensure_header_and_cache_sync() -> dict[str, int]:
    """Garantiza el header y construye el mapa email->fila. Sincrono."""
    svc = _get_service()
    sid = settings.google_sheet_id
    tab = _tab()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{tab}!A1:M"
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"{tab}!A1",
            valueInputOption="RAW", body={"values": [HEADERS]},
        ).execute()
        return {}
    mapping: dict[str, int] = {}
    for i, row in enumerate(rows[1:], start=2):  # fila 1 = header
        if len(row) > _EMAIL_COL and row[_EMAIL_COL]:
            mapping[row[_EMAIL_COL].strip().lower()] = i
    return mapping


def _upsert_sync(lead) -> None:
    global _email_rows
    svc = _get_service()
    sid = settings.google_sheet_id
    tab = _tab()
    if _email_rows is None:
        _email_rows = _ensure_header_and_cache_sync()
    email = (lead.email or "").lower()
    row_values = _lead_to_row(lead)
    if email and email in _email_rows:
        r = _email_rows[email]
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"{tab}!A{r}:N{r}",
            valueInputOption="RAW", body={"values": [row_values]},
        ).execute()
    else:
        resp = svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"{tab}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        ).execute()
        # Deducir la fila recién insertada del rango de la respuesta.
        updated_range = resp.get("updates", {}).get("updatedRange", "")
        if email:
            try:
                r = int(updated_range.split("!A")[-1].split(":")[0])
                _email_rows[email] = r
            except (ValueError, IndexError):
                _email_rows[email] = -1  # marca presencia aunque no sepamos la fila


# ======================================================================
# API pública (async)
# ======================================================================

async def sync_lead(lead) -> bool:
    """Upsert del lead en el Sheet (no-op si no está configurado)."""
    if not settings.sheets_enabled:
        return False
    try:
        async with _lock:
            await asyncio.to_thread(_upsert_sync, lead)
        return True
    except Exception as exc:  # noqa: BLE001 - la sync no debe tumbar el flujo
        logger.warning("No se pudo sincronizar el lead %s al Sheet: %s",
                       getattr(lead, "id", "?"), exc)
        return False


async def sync_lead_by_id(lead_id: int) -> bool:
    """Re-lee el lead por id y lo sincroniza al Sheet (usado tras cambios de
    estado: email_sent, has_replied, meeting_scheduled)."""
    if not settings.sheets_enabled:
        return False
    from database import crud
    from database.db import session_scope
    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        if lead is None:
            return False
        snapshot = lead
    return await sync_lead(snapshot)


async def email_in_sheet(email: str) -> bool:
    """True si el correo YA está en el Sheet (para dedup cero-duplicados)."""
    global _email_rows
    if not settings.sheets_enabled or not email:
        return False
    try:
        async with _lock:
            if _email_rows is None:
                _email_rows = await asyncio.to_thread(_ensure_header_and_cache_sync)
        return email.lower() in _email_rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo consultar el Sheet para dedup: %s", exc)
        return False  # ante error, no bloquear (la DB sigue siendo el candado duro)
