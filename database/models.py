# database/models.py
"""
Esquema de la base de datos del motor de prospeccion de Jofra.

Tablas:
- leads:           el prospecto y su estado en la maquina de estados.
- chat_messages:   TODO mensaje de WhatsApp (entrante, del bot, o del humano).
                   Es tambien el registro que permite la deteccion de
                   intervencion humana por doble via (Modo Hibrido).
- outbound_queue:  cola asincrona del Agente 3 con retraso humano (1-5 min).
- state_logs:      auditoria de cada transicion de estado de un lead.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.db import Base


def utcnow() -> datetime:
    """Timestamp UTC consistente para toda la DB."""
    return datetime.now(timezone.utc)


# ======================================================================
# ENUMS
# ======================================================================

class LeadStatus(str, enum.Enum):
    """Maquina de estados del lead (gestionada por el Agente 0)."""

    NEW = "new"
    QUALIFYING = "qualifying"
    READY_FOR_OUTREACH = "ready_for_outreach"
    IN_CONVERSATION = "in_conversation"
    MEETING_SCHEDULED = "meeting_scheduled"
    DISCARDED = "discarded"  # red flags: coworking, plaza comercial, etc.


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"    # el lead nos escribe
    OUTBOUND = "outbound"  # nosotros (bot o Francisco) escribimos al lead


class SenderType(str, enum.Enum):
    LEAD = "lead"                # el prospecto
    BOT = "bot"                  # Agentes 3 / 4 via Cloud API
    HUMAN_AGENT = "human_agent"  # Francisco desde su telefono (Modo Hibrido)


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class OutboundStatus(str, enum.Enum):
    PENDING = "pending"      # en cola, esperando su retraso humano
    SENT = "sent"
    CANCELLED = "cancelled"  # p.ej. el humano tomo la conversacion
    FAILED = "failed"


# ======================================================================
# TABLAS
# ======================================================================

class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    
    # Contexto personalizado generado por el Enricher
    icebreaker_context: Mapped[str | None] = mapped_column(Text)
    
    # Pre-redacción del correo (Agente 3)
    draft_subject: Mapped[str | None] = mapped_column(Text)
    draft_body: Mapped[str | None] = mapped_column(Text)
    
    # Probabilidad de compra estimada por el Enricher (ej. Alta, Media, Baja)
    purchase_probability: Mapped[str | None] = mapped_column(String(32))

    # ------------------ CANAL DE CORREO (Cold Email) ------------------
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    has_replied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    meeting_scheduled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # Datos de prospeccion (Agentes 1 y 2)
    company_name: Mapped[str | None] = mapped_column(String(255), index=True)
    contact_name: Mapped[str | None] = mapped_column(String(255))
    contact_role: Mapped[str | None] = mapped_column(String(255))  # Director General, Mantenimiento...
    website: Mapped[str | None] = mapped_column(String(512))
    sector: Mapped[str | None] = mapped_column(String(128))  # frigorifico, hotel, CEDIS...
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str | None] = mapped_column(String(128))  # google_dorking, maps_scraping...

    # Maquina de estados
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, values_callable=lambda e: [m.value for m in e]),
        default=LeadStatus.NEW,
        index=True,
        nullable=False,
    )

    # Resultado de calificacion (Agente 2)
    qualification_notes: Mapped[str | None] = mapped_column(Text)
    red_flags: Mapped[str | None] = mapped_column(Text)

    # ------------------ MODO HIBRIDO ------------------
    # True => Francisco (humano) tomo la conversacion. Los Agentes 3 y 4
    # quedan BLOQUEADOS para este lead hasta liberacion manual.
    is_human_agent_assigned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    human_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    outbound_items: Mapped[list["OutboundQueueItem"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    state_logs: Mapped[list["StateLog"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Lead {self.id} | Email: {self.email} | "
            f"Status: {self.status.value}>"
        )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Para vincular respuestas de correo al hilo si fuera necesario
    email_message_id: Mapped[str | None] = mapped_column(String(255))

    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    sender_type: Mapped[SenderType] = mapped_column(
        Enum(SenderType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    message_type: Mapped[str] = mapped_column(String(32), default="text")  # text, image, audio...
    body: Mapped[str | None] = mapped_column(Text)

    # Payload crudo del webhook (JSON serializado) para auditoria/debug.
    raw_payload: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    lead: Mapped["Lead"] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ChatMessage id={self.id} lead={self.lead_id} "
            f"{self.direction.value}/{self.sender_type.value}>"
        )


class OutboundQueueItem(Base):
    """Cola del Agente 3: mensajes redactados que esperan su retraso humano
    aleatorio (1-5 min) antes de enviarse por la Cloud API."""

    __tablename__ = "outbound_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Nullable: el mensaje se GENERA con el Agente 3 despues del retraso
    # humano (no al encolar), por si el lead responde o un humano interviene
    # durante la espera y ya no hay que gastar tokens generandolo.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Momento programado de envio (ya incluye el jitter humano)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[OutboundStatus] = mapped_column(
        Enum(OutboundStatus, values_callable=lambda e: [m.value for m in e]),
        default=OutboundStatus.PENDING,
        index=True,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_message_id: Mapped[str | None] = mapped_column(String(255), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    lead: Mapped["Lead"] = relationship(back_populates="outbound_items")


class StateLog(Base):
    """Auditoria: cada transicion de la maquina de estados queda registrada."""

    __tablename__ = "state_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )

    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # Quien provoco la transicion: agent_0, agent_2, webhook, human, system...
    actor: Mapped[str] = mapped_column(String(64), default="system", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    lead: Mapped["Lead"] = relationship(back_populates="state_logs")


class User(Base):
    """Tabla de usuarios del sistema con roles y verificacion."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda e: [m.value for m in e]),
        default=UserRole.USER,
        nullable=False,
    )
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    special_code_used: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

