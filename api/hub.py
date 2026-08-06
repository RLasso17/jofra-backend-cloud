# api/hub.py
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import ChatMessage, Lead, LeadStatus, StateLog

from config.settings import BASE_DIR

router = APIRouter(tags=["hub"])
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/hub", response_class=HTMLResponse)
async def get_hub_ui(request: Request):
    """Renderiza el Frontend de Jofra Hub."""
    return templates.TemplateResponse(request=request, name="hub.html")


@router.get("/admin/export-excel", tags=["ops"])
async def export_excel(ids: str = None):
    """Genera el reporte_leads.xlsx con el estado de todos los leads o los seleccionados."""
    from export_to_excel import export_leads_to_excel
    from fastapi.responses import FileResponse
    
    lead_ids = None
    if ids:
        lead_ids = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]

    path = export_leads_to_excel(ids=lead_ids)
    return FileResponse(path, filename="reporte_leads_jofra.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.get("/api/leads")
async def get_all_leads(db: Session = Depends(get_db)) -> dict:
    """Devuelve la lista de leads para poblar Data Grid y Kanban."""
    # Restaurar cualquier lead enviado que haya sido marcado como discarded por error
    try:
        from database.models import LeadStatus
        sent_discarded = db.scalars(
            select(Lead).where(
                Lead.email_sent.is_(True),
                Lead.has_replied.is_(False),
                Lead.status == LeadStatus.DISCARDED
            )
        ).all()
        if sent_discarded:
            for l in sent_discarded:
                l.status = LeadStatus.READY_FOR_OUTREACH
            db.commit()
    except Exception:
        pass

    leads = db.scalars(select(Lead).order_by(Lead.updated_at.desc())).all()
    
    result = []
    for l in leads:
        result.append({
            "id": l.id,
            "empresa": l.company_name or "Sin Empresa",
            "contacto": l.contact_name or "Sin Nombre",
            "puesto": l.contact_role or "",
            "sector": l.sector or "N/A",
            "ciudad": l.city or "N/A",
            "correo": l.email or "",
            "icebreaker": l.icebreaker_context or "",
            "draft_subject": l.draft_subject or "",
            "draft_body": l.draft_body or "",
            "prob": l.purchase_probability or "Media",
            "estado": l.status.value,
            "enviado": "Sí" if l.email_sent else "No",
            "contesto": "Sí" if l.has_replied else "No",
            "reunion": "Sí" if l.meeting_scheduled else "No",
            "actualizado": l.updated_at.strftime("%b %d %H:%M") if l.updated_at else "",
            "human_assigned": l.is_human_agent_assigned,
        })
    return {"leads": result}


@router.get("/api/leads/{lead_id}/thread")
async def get_lead_thread(lead_id: int, db: Session = Depends(get_db)) -> dict:
    """Devuelve todo el hilo cronológico de correos de un lead específico."""
    messages = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.lead_id == lead_id)
        .order_by(ChatMessage.created_at.asc())
    ).all()
    
    thread = []
    for m in messages:
        thread.append({
            "id": m.id,
            "direction": m.direction.value,
            "sender_type": m.sender_type.value,
            "body": m.body or "",
            "date": m.created_at.strftime("%b %d %H:%M") if m.created_at else ""
        })
    return {"thread": thread}


@router.get("/api/kpis")
async def get_kpis(db: Session = Depends(get_db)) -> dict:
    """Devuelve estadísticas y conversiones."""
    
    total_leads = db.scalar(select(func.count(Lead.id))) or 0
    total_qualified = db.scalar(select(func.count(Lead.id)).where(Lead.status == LeadStatus.READY_FOR_OUTREACH.value)) or 0
    total_discarded = db.scalar(select(func.count(Lead.id)).where(Lead.status == LeadStatus.DISCARDED.value)) or 0
    total_sent = db.scalar(select(func.count(Lead.id)).where(Lead.email_sent == True)) or 0
    total_replied = db.scalar(select(func.count(Lead.id)).where(Lead.has_replied == True)) or 0
    total_meetings = db.scalar(select(func.count(Lead.id)).where(Lead.meeting_scheduled == True)) or 0
    
    # Asumimos ticket promedio de $200,000 MXN por proyecto para motivacion
    pipeline_value = total_qualified * 200000
    
    # Conversión por sector
    from sqlalchemy import Integer, cast
    sector_stats = db.execute(
        select(Lead.sector, func.count(Lead.id), func.sum(cast(Lead.has_replied, Integer)))
        .group_by(Lead.sector)
        .where(Lead.sector != None)
    ).all()
    
    sectores = []
    for s_name, s_count, s_replied in sector_stats:
        if s_count > 0:
            sectores.append({
                "sector": s_name, 
                "total": s_count, 
                "replied": s_replied or 0,
                "conv": round(((s_replied or 0) / s_count) * 100, 1)
            })
    sectores = sorted(sectores, key=lambda x: x["conv"], reverse=True)

    return {
        "total": total_leads,
        "qualified": total_qualified,
        "discarded": total_discarded,
        "sent": total_sent,
        "replied": total_replied,
        "meetings": total_meetings,
        "pipeline_value": pipeline_value,
        "sectors": sectores
    }


@router.get("/api/agent-logs")
async def get_agent_logs(db: Session = Depends(get_db)) -> dict:
    """Traduce los StateLogs crudos a lenguaje natural amigable para el Agent Hub."""
    logs = db.scalars(select(StateLog).order_by(StateLog.created_at.desc()).limit(20)).all()
    
    friendly_logs = []
    for log in logs:
        text = ""
        # Traductor simple de eventos de StateLog
        if log.to_status == LeadStatus.NEW.value:
            text = f"Lead de prueba cargado o extraído: {log.reason}"
        elif log.to_status == LeadStatus.READY_FOR_OUTREACH.value:
            text = f"Agente Enricher analizó perfil y generó context. Listo para correo."
        elif log.to_status == LeadStatus.IN_CONVERSATION.value:
            if "HUMAN" in (log.reason or ""):
                text = f"El Humano tomó control del chat."
            else:
                text = f"¡Respuesta detectada en bandeja! Movido a conversación."
        elif log.to_status == LeadStatus.MEETING_SCHEDULED.value:
            text = f"¡Cita agendada con éxito por el Agente 4!"
        elif log.from_status == log.to_status and log.actor == "hybrid_mode":
             text = log.reason # Ya es legible
        else:
            text = f"Actualización: {log.reason}"
            
        friendly_logs.append({
            "time": log.created_at.strftime("%H:%M"),
            "agent": log.actor.upper(),
            "text": text
        })
        
    return {"logs": friendly_logs}


@router.post("/api/leads/{lead_id}/mark-urgent")
async def mark_lead_urgent(lead_id: int, db: Session = Depends(get_db)):
    """Pausa al bot indefinidamente marcando la bandera is_human_agent_assigned."""
    from database import crud
    
    lead = crud.get_lead_by_id(db, lead_id)
    if lead:
        crud.assign_human_agent(db, lead, reason="Marcado como URGENTE desde Kanban")
    return {"status": "ok"}


import os
import json

@router.get("/api/debug-queue")
async def debug_queue(db: Session = Depends(get_db)):
    from database.models import OutboundQueueItem
    from sqlalchemy import select
    items = db.scalars(select(OutboundQueueItem).order_by(OutboundQueueItem.id.desc()).limit(20)).all()
    res = []
    for it in items:
        res.append({
            "id": it.id,
            "lead_id": it.lead_id,
            "status": it.status.value if hasattr(it.status, "value") else str(it.status),
            "scheduled_at": str(it.scheduled_at),
            "last_error": it.last_error
        })
    return {"items": res}

STATE_FILE = BASE_DIR / "system_state.json"

@router.get("/api/system-state")
async def get_system_state():
    is_active = True
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                is_active = json.load(f).get("is_active", True)
        except:
            pass
    else:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"is_active": True}, f)
        except:
            pass
    return {"is_active": is_active}

@router.post("/api/toggle-system")
async def toggle_system():
    is_active = True
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                is_active = json.load(f).get("is_active", True)
        except:
            pass
    
    new_state = not is_active
    with open(STATE_FILE, "w") as f:
        json.dump({"is_active": new_state}, f)
        
    if new_state:
        try:
            from orchestration.outreach_worker import schedule_all_ready_outreach
            schedule_all_ready_outreach()
            
            # Verificación de cuota diaria (Catch-Up): Si no se han completado los 150 leads hoy, ejecutar extracción inmediatamente
            import asyncio
            from datetime import datetime, timezone
            from database.db import session_scope
            from database.models import Lead
            from sqlalchemy import select, func
            
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            with session_scope() as db:
                count_today = db.scalar(select(func.count(Lead.id)).where(Lead.created_at >= today_start)) or 0
                
            if count_today < 150:
                from scripts.run_daily_extraction import run_daily_extraction
                asyncio.create_task(run_daily_extraction(150))
        except Exception as exc:
            pass

    return {"is_active": new_state}
        
@router.put("/api/leads/{lead_id}/draft")
async def update_lead_draft(lead_id: int, request: Request, db: Session = Depends(get_db)):
    from database import crud
    data = await request.json()
    lead = crud.get_lead_by_id(db, lead_id)
    if not lead:
        return {"status": "error", "message": "Lead not found"}
    
    lead.draft_subject = data.get("draft_subject", lead.draft_subject)
    lead.draft_body = data.get("draft_body", lead.draft_body)
    db.commit()
    return {"status": "ok"}


# ======================================================================
# RUTAS DE AUTENTICACIÓN Y USUARIOS (CÓDIGO MASTER: PASSO)
# ======================================================================

def _notify_admin_email(subject: str, body: str):
    """Notifica las verificaciones/registros de usuarios a rlassoa17@gmail.com."""
    try:
        from tools.email_channel.email_sender import _send_sync
        _send_sync("rlassoa17@gmail.com", subject, body)
    except Exception as e:
        print(f"Error enviando correo de notificacion a admin: {e}")

@router.post("/api/auth/register")
async def register_user(request: Request, db: Session = Depends(get_db)):
    from database import crud
    from database.models import UserRole
    data = await request.json()
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "")
    code = data.get("special_code", "").strip().upper()
    
    if not username or not email or not password:
        return {"status": "error", "message": "Faltan campos obligatorios."}
        
    existing = crud.get_user_by_username(db, username) or crud.get_user_by_email(db, email)
    if existing:
        return {"status": "error", "message": "El usuario o correo ya existe."}
        
    role = UserRole.ADMIN if code == "PASSO" else UserRole.USER
    user = crud.create_user(db, username, email, password, role=role, special_code=code if code else None)
    db.commit()
    
    # Notificar a rlassoa17@gmail.com
    _notify_admin_email(
        f"Nuevo Registro de Usuario en Jofra Hub: {username}",
        f"Se ha registrado el usuario {username} ({email}).\nRol otorgado: {role.value.upper()}\nCódigo especial usado: {code if code else 'Ninguno'}\nEstado: APROBADO Y VERIFICADO."
    )
    
    return {
        "status": "ok",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role.value
        }
    }

@router.post("/api/auth/login")
async def login_user(request: Request, db: Session = Depends(get_db)):
    from database import crud
    from database.models import UserRole
    data = await request.json()
    username_or_email = data.get("username", "").strip()
    password = data.get("password", "")
    code = data.get("special_code", "").strip().upper()
    
    if not username_or_email or not password:
        return {"status": "error", "message": "Faltan credenciales."}
        
    user = crud.verify_user_credentials(db, username_or_email, password)
    
    # Si no existe pero usaron PASSO o credenciales admin por defecto, crearlo
    if not user:
        if code == "PASSO" or username_or_email.lower() in ["admin", "rlassoa17@gmail.com"]:
            user = crud.create_user(db, username_or_email, f"{username_or_email.lower()}@jofra.com" if "@" not in username_or_email else username_or_email, password, role=UserRole.ADMIN, special_code=code)
            db.commit()
        else:
            return {"status": "error", "message": "Credenciales inválidas."}

    # Si ingresaron PASSO en el login, elevar automáticamente a Admin
    if code == "PASSO" and user.role != UserRole.ADMIN:
        user.role = UserRole.ADMIN
        user.special_code_used = code
        db.commit()
        
    _notify_admin_email(
        f"Inicio de Sesión en Jofra Hub: {user.username}",
        f"El usuario {user.username} ({user.email}) ha iniciado sesión.\nRol: {user.role.value.upper()}\nCódigo usado: {code if code else 'Normal'}"
    )

    return {
        "status": "ok",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role.value
        }
    }

@router.post("/api/auth/google")
async def google_login(request: Request, db: Session = Depends(get_db)):
    from database import crud
    from database.models import UserRole
    data = await request.json()
    email = data.get("email", "usuario_google@gmail.com").strip().lower()
    name = data.get("name", "Usuario Google").strip()
    code = data.get("special_code", "").strip().upper()
    
    user = crud.get_user_by_email(db, email)
    if not user:
        role = UserRole.ADMIN if (code == "PASSO" or email == "rlassoa17@gmail.com") else UserRole.USER
        username = email.split("@")[0]
        user = crud.create_user(db, username, email, "google_oauth_pass", role=role, special_code=code if code else None)
        db.commit()
    elif code == "PASSO" and user.role != UserRole.ADMIN:
        user.role = UserRole.ADMIN
        db.commit()
        
    _notify_admin_email(
        f"Inicio de Sesión Google en Jofra Hub: {email}",
        f"El usuario {name} ({email}) ha iniciado sesión vía Google.\nRol: {user.role.value.upper()}\nCódigo usado: {code if code else 'Ninguno'}"
    )

    return {
        "status": "ok",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role.value
        }
    }

@router.get("/api/admin/users")
async def list_users(db: Session = Depends(get_db)):
    from database import crud
    users = crud.get_all_users(db)
    res = []
    for u in users:
        res.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "role": u.role.value,
            "is_approved": u.is_approved,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else ""
        })
    return {"users": res}

