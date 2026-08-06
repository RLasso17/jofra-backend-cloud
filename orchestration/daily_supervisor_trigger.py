# orchestration/daily_supervisor_trigger.py
"""
Trigger diario del Agente Supervisor (Arquitecto en Jefe).
1. Recopila métricas de la base de datos (rendimiento, tasas de apertura, rechazos).
2. Extrae los Experience Logs (+1 / -1) para ver tácticas exitosas y fallidas.
3. Invoca al Supervisor (Agente 5) para que evalúe y redacte una nueva estrategia dinámica.
4. Sobrescribe `knowledge/learned_strategy.md`.
5. Envía un reporte diario por correo al administrador (rlassoa17@gmail.com).
"""

import asyncio
import logging
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agents.supervisor.agent import supervisor_agent
from database import crud
from database.db import session_scope
from tools.email_channel.email_sender import send_email

logger = logging.getLogger(__name__)

async def _run_supervisor(stats_text: str) -> str:
    runner = Runner(
        app_name="jofra_prospecting",
        agent=supervisor_agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    
    prompt = (
        "Evalúa las siguientes estadísticas y logs de experiencia del día de hoy (o recientes). "
        "Basado en esta evidencia empírica, redacta la nueva estrategia (formato Markdown) "
        "indicando explícitamente QUÉ nichos atacar y CÓMO abordarlos, así como QUÉ evitar. "
        "Recuerda: tu salida será escrita directamente en `learned_strategy.md`.\n\n"
        f"{stats_text}"
    )
    
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    async for event in runner.run_async(user_id="supervisor", session_id="daily_analysis", new_message=content):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text += part.text
                    
    return final_text.strip()

async def run_daily_supervision():
    logger.info("Iniciando supervisión y auto-aprendizaje diario...")
    
    # 1. Recopilar datos (simplificado: logs de experiencia recientes)
    stats_text = "== EXPERIENCIAS DEL DÍA ==\n"
    with session_scope() as db:
        logs = crud.get_recent_experiences(db, limit=50)
        wins = [l for l in logs if l.score > 0]
        losses = [l for l in logs if l.score < 0]
        
        stats_text += f"ÉXITOS TOTALES (Citas Agendadas): {len(wins)}\n"
        for w in wins:
            stats_text += f"[EXITO] {w.agent_name} - {w.feedback_text}\nContexto: {w.action_context}\n\n"
            
        stats_text += f"FRACASOS TOTALES (Rechazos Directos): {len(losses)}\n"
        for l in losses:
            stats_text += f"[FRACASO] {l.agent_name} - {l.feedback_text}\nContexto: {l.action_context}\n\n"
            
    # 2. Invocar Supervisor
    new_strategy = await _run_supervisor(stats_text)
    
    # 3. Sobrescribir learned_strategy.md
    learned_path = Path.cwd() / "knowledge" / "learned_strategy.md"
    learned_path.parent.mkdir(parents=True, exist_ok=True)
    with open(learned_path, "w", encoding="utf-8") as f:
        f.write(new_strategy)
        
    logger.info("learned_strategy.md actualizado exitosamente.")
    
    # 4. Enviar reporte por email
    report_body = (
        "Hola,\n\n"
        "El Agente Supervisor ha concluido su evaluación diaria del sistema de prospección autónoma.\n"
        f"Se registraron {len(wins)} éxitos y {len(losses)} rechazos.\n\n"
        "El archivo learned_strategy.md ha sido actualizado con los nuevos lineamientos tácticos y será consumido "
        "por el Orquestador y el Outreach Worker a partir de mañana.\n\n"
        "=== NUEVA ESTRATEGIA (VISTA PREVIA) ===\n\n"
        f"{new_strategy[:1000]}...\n\n"
        "Saludos,\nEl Supervisor (Agente 5)"
    )
    
    await send_email(
        to_email="rlassoa17@gmail.com",
        subject="Reporte Diario Jofra - Actualización de Estrategia (Self-Refining RAG)",
        body=report_body
    )
    logger.info("Reporte enviado a administrador.")

if __name__ == "__main__":
    asyncio.run(run_daily_supervision())
