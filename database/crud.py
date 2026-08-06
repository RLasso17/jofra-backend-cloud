# database/crud.py
"""
Operaciones de base de datos del sistema.

Reglas de diseno:
- Toda funcion recibe la Session como primer argumento (quien llama controla
  la transaccion via session_scope() o la dependency get_db()).
- Las transiciones de estado SIEMPRE pasan por transition_lead_status() para
  que queden auditadas en state_logs y se respete la maquina de estados.
- La asignacion de humano SIEMPRE pasa por assign_human_agent(), que ademas
  cancela la cola de outreach pendiente (el bot se calla de inmediato).
"""

import json
import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, case
from sqlalchemy.orm import Session

from database.models import (
    ChatMessage,
    Lead,
    LeadStatus,
    MessageDirection,
    OutboundQueueItem,
    OutboundStatus,
    SenderType,
    StateLog,
)

logger = logging.getLogger(__name__)

# Maquina de estados valida (Agente 0). Cualquier salto fuera de este mapa
# requiere force=True y queda marcado en el log de auditoria.
ALLOWED_TRANSITIONS: dict[LeadStatus, set[LeadStatus]] = {
    # El Lead Finder ahora filtra/califica inline, así que un lead nuevo puede
    # pasar directo a ready_for_outreach (sin el paso intermedio de qualifying).
    LeadStatus.NEW: {LeadStatus.QUALIFYING, LeadStatus.READY_FOR_OUTREACH, LeadStatus.DISCARDED},
    LeadStatus.QUALIFYING: {LeadStatus.READY_FOR_OUTREACH, LeadStatus.DISCARDED},
    LeadStatus.READY_FOR_OUTREACH: {LeadStatus.IN_CONVERSATION, LeadStatus.DISCARDED},
    LeadStatus.IN_CONVERSATION: {LeadStatus.MEETING_SCHEDULED, LeadStatus.DISCARDED},
    LeadStatus.MEETING_SCHEDULED: set(),
    LeadStatus.DISCARDED: set(),
}


class InvalidStateTransition(Exception):
    """Transicion de estado no permitida por la maquina de estados."""


# ======================================================================
# LEADS
# ======================================================================

def get_lead_by_id(db: Session, lead_id: int) -> Lead | None:
    return db.get(Lead, lead_id)


def get_lead_by_email(db: Session, email: str) -> Lead | None:
    return db.scalar(select(Lead).where(Lead.email == email.lower()))


def create_lead(
    db: Session,
    *,
    company_name: str | None = None,
    contact_name: str | None = None,
    contact_role: str | None = None,
    email: str | None = None,
    website: str | None = None,
    sector: str | None = None,
    city: str | None = None,
    state: str | None = None,
    source: str | None = None,
    icebreaker_context: str | None = None,
    purchase_probability: str | None = None,
) -> Lead:
    lead = Lead(
        company_name=company_name,
        contact_name=contact_name,
        contact_role=contact_role,
        email=email.lower() if email else None,
        website=website,
        sector=sector,
        city=city,
        state=state,
        source=source,
        icebreaker_context=icebreaker_context,
        purchase_probability=purchase_probability,
        status=LeadStatus.NEW,
    )
    db.add(lead)
    db.flush()  # asigna lead.id sin cerrar la transaccion
    db.add(
        StateLog(
            lead_id=lead.id,
            from_status=None,
            to_status=LeadStatus.NEW.value,
            reason="Lead creado",
            actor=source or "system",
        )
    )
    logger.info(
        "Lead %s creado (%s | %s)",
        lead.id, company_name, email,
    )
    return lead


# ---------------- Canal de Cold Email ----------------

def set_email(db: Session, lead: Lead, email: str) -> Lead:
    """Asigna/actualiza el correo del lead."""
    lead.email = email.lower()
    db.flush()
    return lead


def redirect_lead_contact(
    db: Session, lead: Lead, new_email: str, new_contact_name: str | None = None
) -> Lead:
    """Redirección de contacto (referral): el buzón general nos dio el correo
    directo del decisor. Cambia el correo del lead al nuevo y actualiza el 
    nombre del contacto si se proporcionó.
    """
    old_email = lead.email
    lead.email = new_email.lower()
    if new_contact_name:
        lead.contact_name = new_contact_name
    db.flush()
    logger.info(
        "Lead %s REDIRIGIDO: %s -> %s (%s)",
        lead.id, old_email, lead.email, new_contact_name or "contacto directo",
    )
    return lead


def set_draft_email(db: Session, lead: Lead, subject: str, body: str) -> None:
    lead.draft_subject = subject
    lead.draft_body = body
    db.commit()
    db.refresh(lead)


def mark_email_sent(db: Session, lead: Lead) -> Lead:
    """Marca que ya se le envio el cold email al lead."""
    lead.email_sent = True
    db.flush()
    return lead


def mark_has_replied(db: Session, lead: Lead) -> Lead:
    """Marca que el lead respondio al correo."""
    lead.has_replied = True
    db.flush()
    return lead


def mark_meeting_scheduled(db: Session, lead: Lead) -> Lead:
    """Marca que ya se agendo la reunion con el lead."""
    lead.meeting_scheduled = True
    db.flush()
    return lead


def _get_priority_case():
    return case(
        (Lead.purchase_probability == "Muy Alta", 1),
        (Lead.purchase_probability == "Alta", 2),
        (Lead.purchase_probability == "Media", 3),
        (Lead.purchase_probability == "Baja", 4),
        (Lead.purchase_probability == "Muy Baja", 5),
        else_=6
    )


def get_leads_ready_for_outreach(db: Session, limit: int = 500) -> list[Lead]:
    """Leads en ready_for_outreach a los que AÚN no se les envió el cold email."""
    return list(
        db.scalars(
            select(Lead)
            .where(Lead.status == LeadStatus.READY_FOR_OUTREACH, Lead.email_sent.is_(False))
            .order_by(_get_priority_case(), Lead.id.asc())
            .limit(limit)
        ).all()
    )


def get_or_create_lead_from_email(
    db: Session,
    *,
    email: str,
    company_name: str | None = None,
    contact_name: str | None = None,
) -> Lead:
    """Resuelve el lead de un correo entrante (respuesta del prospecto).

    Si quien responde no existe en la DB (raro, pero posible con reenvios), se
    crea para no perder la conversacion.
    """
    lead = get_lead_by_email(db, email)
    if lead:
        return lead
    return create_lead(
        db, email=email, company_name=company_name, contact_name=contact_name,
        source="inbound_email",
    )



def transition_lead_status(
    db: Session,
    lead: Lead,
    new_status: LeadStatus,
    *,
    reason: str | None = None,
    actor: str = "system",
    force: bool = False,
) -> Lead:
    """Unica puerta de entrada para mover un lead en la maquina de estados."""
    old_status = lead.status
    if old_status == new_status:
        return lead

    if not force and new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
        raise InvalidStateTransition(
            f"Lead {lead.id}: transicion {old_status.value} -> {new_status.value} "
            f"no permitida. Validas: "
            f"{[s.value for s in ALLOWED_TRANSITIONS.get(old_status, set())]}"
        )

    lead.status = new_status
    db.add(
        StateLog(
            lead_id=lead.id,
            from_status=old_status.value,
            to_status=new_status.value,
            reason=(reason or "") + (" [FORCED]" if force else ""),
            actor=actor,
        )
    )
    db.flush()
    logger.info(
        "Lead %s: %s -> %s (actor=%s, reason=%s)",
        lead.id, old_status.value, new_status.value, actor, reason,
    )
    return lead


def discard_unresponsive_leads(db: Session, hours: int = 48) -> int:
    """Descarta leads (pasan a DISCARDED) que no han respondido después del tiempo límite."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    leads = db.scalars(
        select(Lead)
        .where(
            Lead.status == LeadStatus.READY_FOR_OUTREACH,
            Lead.email_sent.is_(True),
            Lead.has_replied.is_(False),
            Lead.updated_at <= cutoff
        )
    ).all()
    count = 0
    for lead in leads:
        try:
            transition_lead_status(
                db, lead, LeadStatus.DISCARDED,
                reason=f"Sin respuesta tras {hours}h", actor="cleanup_job",
                force=True
            )
            count += 1
        except InvalidStateTransition:
            pass
    if count > 0:
        db.flush()
        logger.info("Limpieza: %d leads descartados por falta de respuesta (> %dh).", count, hours)
    return count



# ======================================================================
# MODO HIBRIDO
# ======================================================================

def assign_human_agent(db: Session, lead: Lead, *, reason: str) -> Lead:
    """Francisco tomo la conversacion: silencia al bot para este lead.

    Efectos:
    1. is_human_agent_assigned = True (los Agentes 3/4 quedan bloqueados).
    2. Se cancela TODO mensaje pendiente en la cola de outreach.
    3. Queda auditado en state_logs (sin cambiar el status del lead).
    """
    if lead.is_human_agent_assigned:
        return lead  # idempotente: los webhooks de Meta se reintentan

    lead.is_human_agent_assigned = True
    lead.human_assigned_at = datetime.now(timezone.utc)

    cancelled = cancel_pending_outbound(db, lead.id, reason="Intervencion humana detectada")

    db.add(
        StateLog(
            lead_id=lead.id,
            from_status=lead.status.value,
            to_status=lead.status.value,
            reason=f"HUMAN TAKEOVER: {reason}. Outbound cancelados: {cancelled}",
            actor="hybrid_mode",
        )
    )
    db.flush()
    logger.warning(
        "MODO HIBRIDO: lead %s asignado a humano (%s). Bot bloqueado.", lead.id, reason
    )
    return lead


def release_human_agent(db: Session, lead: Lead, *, reason: str) -> Lead:
    """Liberacion manual: el bot puede volver a operar este lead."""
    if not lead.is_human_agent_assigned:
        return lead
    lead.is_human_agent_assigned = False
    lead.human_assigned_at = None
    db.add(
        StateLog(
            lead_id=lead.id,
            from_status=lead.status.value,
            to_status=lead.status.value,
            reason=f"HUMAN RELEASE: {reason}",
            actor="hybrid_mode",
        )
    )
    db.flush()
    logger.info("MODO HIBRIDO: lead %s liberado, el bot puede operar de nuevo.", lead.id)
    return lead


def is_bot_allowed_for_lead(db: Session, lead_id: int) -> bool:
    """Verificacion que DEBEN hacer los Agentes 3 y 4 antes de cualquier accion."""
    lead = db.get(Lead, lead_id)
    if lead is None:
        return False
    return not lead.is_human_agent_assigned


# ======================================================================
# MENSAJES DE CHAT
# ======================================================================

def save_chat_message(
    db: Session,
    *,
    lead_id: int,
    direction: MessageDirection,
    sender_type: SenderType,
    body: str | None,
    email_message_id: str | None = None,
    message_type: str = "text",
    raw_payload: dict | None = None,
) -> ChatMessage:
    """Persiste un mensaje (correo)."""
    msg = ChatMessage(
        lead_id=lead_id,
        email_message_id=email_message_id,
        direction=direction,
        sender_type=sender_type,
        message_type=message_type,
        body=body,
        raw_payload=json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None,
    )
    db.add(msg)
    db.flush()
    return msg



def get_conversation_history(
    db: Session, lead_id: int, limit: int = 50
) -> list[ChatMessage]:
    """Historial cronologico reciente (contexto para el Agente 4)."""
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.lead_id == lead_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


# ======================================================================
# COLA DE OUTREACH (Agente 3)
# ======================================================================

def enqueue_outbound_message(
    db: Session,
    *,
    lead_id: int,
    body: str,
    delay_min_seconds: int,
    delay_max_seconds: int,
) -> OutboundQueueItem:
    """Encola un mensaje con retraso humano aleatorio (anti-ban de WhatsApp).

    El jitter se calcula AQUI y queda fijo en scheduled_at; el worker solo
    compara contra el reloj, asi el retraso sobrevive reinicios del proceso.
    """
    delay = random.randint(delay_min_seconds, delay_max_seconds)
    item = OutboundQueueItem(
        lead_id=lead_id,
        body=body,
        scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
        status=OutboundStatus.PENDING,
    )
    db.add(item)
    db.flush()
    logger.info("Outbound encolado para lead %s con retraso de %ss.", lead_id, delay)
    return item


def enqueue_lead_for_outreach(
    db: Session,
    *,
    lead_id: int,
    delay_min_seconds: int = 1,
    delay_max_seconds: int = 5,
) -> OutboundQueueItem:
    """Encola un LEAD para outreach SIN cuerpo todavia.

    El retraso humano (jitter) se fija aqui en scheduled_at, de modo que
    sobrevive a reinicios del proceso. El mensaje lo redacta el Agente 3
    cuando el item vence, no ahora. Lo llama el flujo cuando el Agente 2
    pasa el lead a ready_for_outreach.
    """
    delay = random.randint(delay_min_seconds, delay_max_seconds)
    item = OutboundQueueItem(
        lead_id=lead_id,
        body=None,
        scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
        status=OutboundStatus.PENDING,
    )
    db.add(item)
    db.flush()
    logger.info(
        "Lead %s encolado para outreach; el mensaje se generara en ~%ss.",
        lead_id, delay,
    )
    return item


def set_outbound_body(db: Session, item: OutboundQueueItem, body: str) -> OutboundQueueItem:
    """Guarda el cuerpo generado por el Agente 3 justo antes de enviar."""
    item.body = body
    db.flush()
    return item


def get_pending_outbound_items(db: Session) -> list[OutboundQueueItem]:
    """Todos los PENDING (para reprogramarlos al arrancar el worker)."""
    return list(
        db.scalars(
            select(OutboundQueueItem)
            .join(Lead, OutboundQueueItem.lead_id == Lead.id)
            .where(OutboundQueueItem.status == OutboundStatus.PENDING)
            .order_by(_get_priority_case(), OutboundQueueItem.scheduled_at)
        ).all()
    )


def get_due_outbound_items(db: Session, now: datetime | None = None) -> list[OutboundQueueItem]:
    """Mensajes PENDING cuyo scheduled_at ya vencio (los consume el worker)."""
    now = now or datetime.now(timezone.utc)
    return list(
        db.scalars(
            select(OutboundQueueItem)
            .join(Lead, OutboundQueueItem.lead_id == Lead.id)
            .where(
                OutboundQueueItem.status == OutboundStatus.PENDING,
                OutboundQueueItem.scheduled_at <= now,
            )
            .order_by(_get_priority_case(), OutboundQueueItem.scheduled_at)
        ).all()
    )


def mark_outbound_sent(
    db: Session, item: OutboundQueueItem, email_message_id: str
) -> OutboundQueueItem:
    """Marca enviado y registra el id del correo."""
    item.status = OutboundStatus.SENT
    item.sent_at = datetime.now(timezone.utc)
    item.email_message_id = email_message_id
    item.attempts += 1
    db.flush()
    return item


def mark_outbound_failed(
    db: Session, item: OutboundQueueItem, error: str
) -> OutboundQueueItem:
    item.attempts += 1
    item.last_error = error
    if item.attempts >= 3:
        item.status = OutboundStatus.FAILED
        logger.error("Outbound %s FALLO definitivo tras %s intentos: %s",
                     item.id, item.attempts, error)
    db.flush()
    return item


def cancel_pending_outbound(db: Session, lead_id: int, *, reason: str) -> int:
    """Cancela todos los PENDING de un lead. Devuelve cuantos cancelo."""
    items = db.scalars(
        select(OutboundQueueItem).where(
            OutboundQueueItem.lead_id == lead_id,
            OutboundQueueItem.status == OutboundStatus.PENDING,
        )
    ).all()
    for item in items:
        item.status = OutboundStatus.CANCELLED
        item.last_error = reason
    db.flush()
    if items:
        logger.info("Cancelados %s outbound pendientes del lead %s (%s).",
                    len(items), lead_id, reason)
    return len(items)


# ======================================================================
# USUARIOS Y AUTENTICACIÓN
# ======================================================================

import hashlib
from database.models import User, UserRole

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == username))

def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email))

def create_user(
    db: Session,
    username: str,
    email: str,
    password_raw: str,
    role: UserRole = UserRole.USER,
    special_code: str | None = None
) -> User:
    user = User(
        username=username.strip(),
        email=email.strip().lower(),
        password_hash=hash_password(password_raw),
        role=role,
        is_approved=True,
        is_verified=True,
        special_code_used=special_code
    )
    db.add(user)
    db.flush()
    return user

def verify_user_credentials(db: Session, username_or_email: str, password_raw: str) -> User | None:
    user = get_user_by_username(db, username_or_email) or get_user_by_email(db, username_or_email.lower())
    if user and user.password_hash == hash_password(password_raw):
        return user
    return None

def get_all_users(db: Session) -> list[User]:
    return db.scalars(select(User).order_by(User.created_at.desc())).all()

def approve_user(db: Session, user_id: int) -> User | None:
    user = db.get(User, user_id)
    if user:
        user.is_approved = True
        db.flush()
    return user

