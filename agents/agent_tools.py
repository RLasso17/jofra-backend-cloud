# agents/agent_tools.py
"""
Wrappers ADK de las tools del sistema (canal: COLD EMAIL).

En ADK el DOCSTRING y la firma de cada funcion son el esquema que ve el LLM
para decidir cuando llamarla. Estas envolturas exponen firmas simples y
docstrings escritos PARA EL MODELO, y delegan en las funciones robustas.

Pivote estrategico: el canal de prospeccion es el CORREO ELECTRONICO. El Lead
Finder busca CORREOS (no telefonos); el Outreach envia cold emails; el Chat
Manager responde correos y agenda. Todas son async y devuelven dict/str.
"""

import logging

from database import crud
from database.db import session_scope
from database.models import LeadStatus
from tools.gcal_integration.google_calendar import schedule_meeting as _schedule_meeting
from tools.sheets import sheets_sync


import re

logger = logging.getLogger(__name__)


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))



async def save_lead(
    company_name: str,
    email: str,
    email_source: str,
    contact_name: str = "",
    contact_role: str = "",
    website: str = "",
    sector: str = "",
    city: str = "",
) -> dict:
    """Guarda un prospecto YA FILTRADO Y CALIFICADO (canal: correo).

    IMPORTANTE: solo llama a save_lead para empresas que YA pasaron tu filtro
    visual del ICP (sector correcto, tamaño relevante, sin red flags) Y de las
    que obtuviste un correo válido. El lead se guarda directamente como
    LISTO PARA OUTREACH (ready_for_outreach): ya no hay paso de calificación.

    Clasifica el origen en email_source:
    - "decision_maker": correo del dueño/director (lo ideal).
    - "company_general": correo general de la empresa (info@, ventas@...).

    El correo se valida y se RECHAZA si es hosting gratuito o formato inválido.

    Args:
        company_name: nombre REAL de la empresa.
        email: correo REAL encontrado (no lo inventes).
        email_source: "decision_maker" o "company_general".
        contact_name, contact_role, website, sector, city: datos reales opcionales.

    Returns:
        dict con saved (bool), lead_id, email, email_source, status y reason.
    """
    email = (email or "").strip().lower()
    if not _is_valid_email(email):
        return {"saved": False, "duplicate": False, "lead_id": None, "email": None,
                "email_source": None, "status": None, "reason": f"Correo inválido: {email!r}"}

    # Candado duro: dominio de hosting gratuito => empresa no seria, no se guarda.
    dom = _classify_domain(website=website, email=email)
    if dom["is_free_hosting"]:
        return {"saved": False, "duplicate": False, "lead_id": None, "email": None,
                "email_source": None, "status": None, "reason": f"Descartado: {dom['reason']}"}

    # ===== DEDUP ESTRICTO: cero duplicados (base de datos + Google Sheet) =====
    with session_scope() as db:
        if crud.get_lead_by_email(db, email) is not None:
            return {"saved": False, "duplicate": True, "lead_id": None, "email": email,
                    "email_source": None, "status": None,
                    "reason": "DUPLICADO: el correo ya está en la base de datos."}
    if await sheets_sync.email_in_sheet(email):
        return {"saved": False, "duplicate": True, "lead_id": None, "email": email,
                "email_source": None, "status": None,
                "reason": "DUPLICADO: el correo ya está en el Google Sheet."}

    # ===== Alta del lead (nace ready_for_outreach) =====
    src = _coerce_source(email_source)
    with session_scope() as db:
        lead = crud.create_lead(
            db, company_name=company_name or None, contact_name=contact_name or None,
            contact_role=contact_role or None, email=email, website=website or None,
            sector=sector or None, city=city or None, source="lead_finder", phone_source=src,
        )
        crud.transition_lead_status(
            db, lead, LeadStatus.READY_FOR_OUTREACH,
            reason="Filtrado por el Lead Finder (bulk)", actor="lead_finder",
        )
        lead_id = lead.id
        snapshot = lead  # expire_on_commit=False -> usable tras cerrar la sesión

    # Sincroniza la fila en el Google Sheet (en vivo).
    await sheets_sync.sync_lead(snapshot)
    return {"saved": True, "duplicate": False, "lead_id": lead_id, "email": email,
            "email_source": src.value, "status": LeadStatus.READY_FOR_OUTREACH.value,
            "reason": "Lead guardado como ready_for_outreach y sincronizado al Sheet."}


async def bulk_prospect_domain(
    domain: str, company_name: str = "", sector: str = "", city: str = "",
    max_contacts: int = 0,
) -> dict:
    """PROSPECCIÓN de un dominio con nuestro MOTOR OSINT PROPIO (sin Hunter).

    El motor busca los nombres de los decisores (dorking en directorios + páginas
    de equipo del sitio), descubre correos (sitio + PDFs), deduce/permuta el
    correo del decisor y lo VALIDA (MX + SMTP opcional). Aplica DEDUP estricto
    (base de datos + Google Sheet) y guarda los NUEVOS como ready_for_outreach,
    sincronizados al Sheet. Devuelve evidencia ICP para que la CALIFIQUES.

    Args:
        domain: dominio de la empresa (ej. "acme.com.mx"), sin http.
        company_name: nombre de la empresa.
        sector, city: contexto opcional.
        max_contacts: cuántos decisores guardar (0 = default).

    Returns:
        dict con domain, saved, duplicates, icp_evidence (para calificar) y count.
    """
    from config.settings import get_settings
    limit = max_contacts or get_settings().max_contacts_per_domain

    engine = await _prospect_domain(domain, company_name=company_name,
                                    sector=sector, city=city, max_contacts=limit)
    candidates = engine.get("candidates", [])
    if not candidates:
        return {"domain": domain, "saved": [], "duplicates": [], "count": 0,
                "icp_evidence": engine.get("icp_evidence", ""),
                "reason": "El motor OSINT no encontró correos para el dominio."}

    saved, duplicates = [], []
    for c in candidates:
        res = await save_lead(
            company_name=company_name or domain, email=c["email"],
            email_source=c.get("email_source", "company_general"),
            contact_name=c.get("name", ""), contact_role=c.get("role", ""),
            website=f"https://{domain}", sector=sector, city=city,
        )
        if res.get("saved"):
            saved.append({"email": c["email"], "name": c.get("name", ""),
                          "role": c.get("role", ""), "confidence": c.get("confidence"),
                          "verified": c.get("verified"), "method": c.get("method")})
        elif res.get("duplicate"):
            duplicates.append(c["email"])

    return {"domain": domain, "saved": saved, "duplicates": duplicates,
            "count": len(saved), "icp_evidence": engine.get("icp_evidence", ""),
            "reason": f"{len(saved)} nuevos, {len(duplicates)} duplicados. "
                      "Revisa icp_evidence para confirmar el ICP."}


# Confianzas aceptables para un correo de C-level "valioso".
_QUALITY_CONFIDENCE = {"alta", "media", "deducido"}


async def hunt_decision_maker(
    domain: str, company_name: str = "", sector: str = "", city: str = "",
) -> dict:
    """CAZA de alta calidad: consigue el correo del TOMADOR DE DECISIONES de un
    dominio con el motor OSINT, y RECHAZA el lead si solo hay correos genéricos.

    Úsala (Agente OSINT Hunter) sobre una empresa YA calificada por el Profiler.
    Corre el motor OSINT (dorking en directorios + PDFs + permutación + validación
    MX/SMTP). FILTRO DE CALIDAD OBLIGATORIO:
      - Solo acepta correos email_source="decision_maker" con confianza
        alta/media/deducido (correo de una PERSONA C-level, no info@/ventas@).
      - Si el motor solo halla correos genéricos o de baja confianza, RECHAZA el
        lead (no lo guarda): "solo queremos correos valiosos".
    Los aprobados pasan por DEDUP estricto (base + Google Sheet) y se guardan
    como ready_for_outreach, sincronizados al Sheet.

    Args:
        domain: dominio de la empresa (ej. "acme.com.mx").
        company_name, sector, city: contexto de la empresa aprobada.

    Returns:
        dict con accepted (bool), saved (lista), duplicates, rejected_reason,
        icp_evidence.
    """
    from config.settings import get_settings
    limit = get_settings().max_contacts_per_domain

    engine = await _prospect_domain(domain, company_name=company_name,
                                    sector=sector, city=city, max_contacts=limit)
    dm = [c for c in engine.get("candidates", [])
          if c.get("email_source") == "decision_maker"
          and c.get("confidence") in _QUALITY_CONFIDENCE]

    if not dm:
        return {"accepted": False, "saved": [], "duplicates": [],
                "rejected_reason": ("Sin correo de C-level de calidad: el motor solo "
                                    "encontró genéricos o baja confianza. Lead RECHAZADO."),
                "icp_evidence": engine.get("icp_evidence", "")}

    saved, duplicates = [], []
    for c in dm:
        res = await save_lead(
            company_name=company_name or domain, email=c["email"],
            email_source="decision_maker", contact_name=c.get("name", ""),
            contact_role=c.get("role", ""), website=f"https://{domain}",
            sector=sector, city=city,
        )
        if res.get("saved"):
            saved.append({"email": c["email"], "name": c.get("name", ""),
                          "role": c.get("role", ""), "confidence": c.get("confidence"),
                          "verified": c.get("verified"), "method": c.get("method")})
        elif res.get("duplicate"):
            duplicates.append(c["email"])

    return {"accepted": len(saved) > 0, "saved": saved, "duplicates": duplicates,
            "rejected_reason": "" if saved else "Todos los correos de calidad ya eran duplicados.",
            "icp_evidence": engine.get("icp_evidence", ""),
            "reason": f"{len(saved)} leads de calidad guardados, {len(duplicates)} duplicados."}




async def validate_email(email: str) -> dict:
    """Valida que un correo tenga formato correcto y sea utilizable.

    Usa esto antes de aprobar un lead: sin un correo válido no hay forma de
    contactarlo en el canal de Cold Email.

    Args:
        email: correo a validar.

    Returns:
        dict con is_valid (bool), email (normalizado) y reason.
    """
    e = (email or "").strip().lower()
    ok = _is_valid_email(e)
    return {"is_valid": ok, "email": e if ok else None,
            "reason": "Correo válido." if ok else "Formato de correo inválido o dominio no usable."}


# ======================================================================
# AGENTE 4 - CHAT MANAGER & CLOSER
# ======================================================================

async def redirect_conversation(
    lead_id: str, new_email: str, new_contact_name: str = ""
) -> dict:
    """Redirige la conversación al contacto directo cuando un buzón general nos
    refiere a otra persona.

    Úsala cuando un correo entrante te diga algo como "para este tema contacta
    al Ing. Carlos en carlos@empresa.com". Actualiza la base de datos: cambia el
    correo del lead al NUEVO, marca el origen como decision_maker (nos dieron el
    contacto directo) y guarda el nombre del nuevo contacto.

    Después de llamar esta herramienta, redacta un correo NUEVO dirigido a esa
    persona (no una respuesta al buzón general). El sistema lo enviará a la nueva
    dirección automáticamente.

    Args:
        lead_id: id del lead de esta conversación (te lo indica el sistema).
        new_email: el nuevo correo directo que nos proporcionaron.
        new_contact_name: nombre de la nueva persona (ej. "Ing. Carlos").

    Returns:
        dict con redirected (bool), new_email, new_contact_name y reason.
    """
    new_email = (new_email or "").strip().lower()
    if not _is_valid_email(new_email):
        return {"redirected": False, "new_email": None, "new_contact_name": None,
                "reason": f"Correo de redirección inválido: {new_email!r}"}
    try:
        lid = int(str(lead_id).strip())
    except (ValueError, TypeError):
        return {"redirected": False, "new_email": None, "new_contact_name": None,
                "reason": f"lead_id inválido: {lead_id!r}"}

    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lid)
        if lead is None:
            return {"redirected": False, "new_email": None, "new_contact_name": None,
                    "reason": f"Lead {lid} no existe."}
        crud.redirect_lead_contact(db, lead, new_email, new_contact_name or None)
        return {"redirected": True, "lead_id": lid, "new_email": new_email,
                "new_contact_name": new_contact_name or None,
                "email_source": "decision_maker",
                "reason": "Contacto redirigido; escribe un correo NUEVO a esta persona."}


async def book_meeting(
    date: str, time: str, client_name: str, client_email: str = ""
) -> dict:
    """Agenda una reunion de 30 min en Google Calendar con link de Google Meet.

    Usa esto SOLO cuando el prospecto ya aceptó reunirse y dio fecha y hora.
    Crea el evento, invita a Francisco Cantú y al prospecto (al correo que
    proporciones), y devuelve el link de Meet para incluirlo en el correo.

    Args:
        date: fecha ("2026-07-15" o "15/07/2026").
        time: hora ("16:00" o "4:00 PM").
        client_name: nombre del prospecto o empresa.
        client_email: correo del prospecto (para enviarle la invitación).

    Returns:
        dict con success, meet_link, event_link, start y error (si lo hubo).
    """
    return await _schedule_meeting(
        date_str=date, time_str=time,
        client_email=client_email or None, client_name=client_name,
    )
