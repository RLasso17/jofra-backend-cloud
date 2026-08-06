# orchestration/runners.py
"""
Runners de ADK para el canal de Cold Email.

- Agente 3 (Outreach): genera el primer correo (asunto + cuerpo). One-shot.
- Agente 4 (Chat Manager): lee la respuesta del prospecto, contesta por correo
  y agenda. Memoria persistente por lead (DatabaseSessionService sobre SQLite).
"""

import logging
import os
import re
import sys
from pathlib import Path

# Garantizar que el directorio raíz de la aplicación esté en sys.path
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.genai import types

from agents.chat_manager.agent import chat_manager_agent
from agents.outreach.agent import outreach_agent
from config.settings import get_settings
from database import crud
from database.db import session_scope
from database.models import LeadStatus, MessageDirection, SenderType
from tools.email_channel.email_sender import send_email
from tools.email_channel.email_sender import send_email


settings = get_settings()
logger = logging.getLogger(__name__)

APP_NAME = "jofra_prospecting"

_outreach_runner: Runner | None = None
_chat_runner: Runner | None = None


def _get_outreach_runner() -> Runner:
    global _outreach_runner
    if _outreach_runner is None:
        _outreach_runner = Runner(
            app_name=APP_NAME, agent=outreach_agent,
            session_service=InMemorySessionService(), auto_create_session=True,
        )
        logger.info("Runner de Outreach (Agente 3) inicializado.")
    return _outreach_runner


def _get_chat_runner() -> Runner:
    global _chat_runner
    if _chat_runner is None:
        _chat_runner = Runner(
            app_name=APP_NAME, agent=chat_manager_agent,
            session_service=DatabaseSessionService(db_url=settings.adk_session_db_url),
            auto_create_session=True,
        )
        logger.info("Runner de Chat Manager (Agente 4) inicializado con memoria SQLite.")
    return _chat_runner


async def _run_and_collect(runner: Runner, user_id: str, session_id: str, prompt: str) -> tuple[str, dict]:
    """Corre un agente y junta (texto_final, {tool_name: respuesta})."""
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    tool_results: dict = {}
    try:
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        final_text += part.text
                    fr = getattr(part, "function_response", None)
                    if fr is not None:
                        tool_results[fr.name] = fr.response
    except Exception as exc:
        logger.warning("Error en llamada LLM de ADK (%s): %s", runner.agent.name, exc)
    return final_text.strip(), tool_results


# ======================================================================
# Brief del lead (para personalizar)
# ======================================================================

def _build_lead_brief(lead) -> str:
    fields = [
        f"Empresa: {lead.company_name}" if lead.company_name else None,
        f"Contacto: {lead.contact_name}" if lead.contact_name else None,
        f"Puesto: {lead.contact_role}" if lead.contact_role else None,
        f"Sector: {lead.sector}" if lead.sector else None,
        f"Ciudad: {lead.city}" if lead.city else None,
        f"Correo destino: {lead.email}" if lead.email else None,
        f"Contexto/Icebreaker: {lead.icebreaker_context}" if lead.icebreaker_context else None,
    ]
    return "\n".join(f for f in fields if f)


# ======================================================================
# AGENTE 3: OUTREACH (Email)
# ======================================================================

def _parse_email_output(raw: str, company_name: str | None = None) -> dict:
    """Pre-procesa la salida raw del Agente 3, remueve CoT/markdown y parsea JSON.

    Devuelve dict {"subject": subject, "body": body}.
    """
    import json
    text = raw.replace("\x00", "").strip()

    json_candidate = None

    # Extract JSON code block first if present
    m_code = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m_code:
        json_candidate = m_code.group(1).strip()
    else:
        # Strip CoT <think>...</think> reasoning blocks prior to main JSON payload
        first_brace = text.find("{")
        if first_brace != -1:
            prefix = text[:first_brace]
            while "<think>" in prefix and "</think>" in prefix:
                new_prefix = re.sub(r"(?s)<think>(?:(?!<think>).)*?</think>", "", prefix)
                if new_prefix == prefix:
                    break
                prefix = new_prefix
            text_clean = prefix + text[first_brace:]
            m_json = re.search(r"\{[\s\S]*\}", text_clean)
            if m_json:
                json_candidate = m_json.group(0).strip()
            else:
                json_candidate = text_clean
        else:
            # No JSON start brace, strip think tags in raw text
            while "<think>" in text and "</think>" in text:
                new_text = re.sub(r"(?s)<think>(?:(?!<think>).)*?</think>", "", text)
                if new_text == text:
                    break
                text = new_text
            json_candidate = text

    subject = ""
    body = ""

    # Attempt direct JSON parse
    if json_candidate:
        try:
            data = json.loads(json_candidate)
            if isinstance(data, dict):
                subject = str(data.get("subject", "")).strip()
                body = str(data.get("body", "")).strip()
        except Exception:
            pass

    if not subject or not body:
        m_json = re.search(r"\{[\s\S]*\}", text)
        if m_json:
            try:
                data = json.loads(m_json.group(0))
                if isinstance(data, dict):
                    subject = subject or str(data.get("subject", "")).strip()
                    body = body or str(data.get("body", "")).strip()
            except Exception:
                pass

    if not subject or not body:
        m_subj = re.search(r"(?im)^\s*ASUNTO\s*:\s*(.+)$", text)
        if m_subj:
            subject = subject or m_subj.group(1).strip()
            rest = text[m_subj.end():]
            rest = re.sub(r"^\s*-{2,}\s*", "", rest.lstrip("\n"), count=1)
            body = body or rest.strip()

    if not subject:
        subject = f"Propuesta de ahorro en CFE para {company_name}" if company_name else "Ahorro de energía para su empresa"

    subject = subject[:200]
    if not body:
        body = text.strip()

    # Strip <think> and </think> delimiters without erasing enclosed text
    subject = re.sub(r"</?think>", "", subject).strip()
    subject = re.sub(r"(?i)^(Thinking|Analysis):\s*", "", subject).strip()
    body = re.sub(r"</?think>", "", body).strip()

    return {"subject": subject, "body": body}


async def generate_outreach_email(lead_id: int) -> dict:
    """Invoca al Agente 3 para redactar el cold email. Devuelve {"subject": subject, "body": body}."""
    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        if lead is None:
            return {"subject": "Ahorro de energía para su empresa", "body": "Estimado Director,\n\n¿Le interesaría explorar opciones para reducir el costo de CFE en su empresa?\n\nFrancisco Cantú\nJofra Sistemas y Equipos"}
        brief = _build_lead_brief(lead)
        company = lead.company_name or "su empresa"
        contact = lead.contact_name or "Estimado Director"
        city = lead.city or "México"
        icebreaker = lead.icebreaker_context or f"He notado que empresas en {city} están buscando reducir sus costos fijos operativos."
        key = lead.email or str(lead_id)

    from llm.model_factory import FREE_OLLAMA_OUTREACH_MODELS, outreach_llm
    from google.adk.agents import LlmAgent
    from agents.outreach.agent import INSTRUCTION

    prompt = ("Redacta el cold email para este prospecto en formato JSON "
              "{\"subject\": \"...\", \"body\": \"...\"}.\n\n" + brief)

    # Rotación a través del pool de modelos gratuitos de Ollama
    for model_name in FREE_OLLAMA_OUTREACH_MODELS:
        try:
            agent = LlmAgent(
                name=f"outreach_{model_name.replace(':', '_')}",
                model=outreach_llm(model_name),
                instruction=INSTRUCTION
            )
            runner = Runner(
                app_name=APP_NAME, agent=agent,
                session_service=InMemorySessionService(), auto_create_session=True,
            )
            text, _ = await _run_and_collect(runner, user_id=key, session_id=f"outreach-{lead_id}-{model_name}", prompt=prompt)
            if text:
                parsed = _parse_email_output(text, company)
                if parsed and parsed.get("body"):
                    return parsed
        except Exception as exc:
            logger.warning("Modelo %s falló para lead %s (%s). Intentando siguiente modelo del pool...", model_name, lead_id, exc)
            continue

    # Fallback de personalización inteligente altamente persuasivo basado en el icebreaker del lead
    subject = f"Propuesta de ahorro en CFE para {company}"
    body = (
        f"{contact},\n\n"
        f"{icebreaker} En Jofra instalamos proyectos solares industriales llave en mano "
        f"que recortan hasta un 98% el recibo de CFE, con ROI en 3 a 5 años y deducción 100% de ISR.\n\n"
        f"¿Tendría 15 minutos esta semana para revisar proyecciones financieras con base en su recibo actual?\n\n"
        f"Francisco Cantú\nJofra Sistemas y Equipos"
    )
    return {"subject": subject, "body": body}


# ======================================================================
# AGENTE 4 - leer respuesta, contestar por correo y agendar
# ======================================================================

def _meet_link_from_results(tool_results: dict) -> str | None:
    bm = tool_results.get("book_meeting")
    if isinstance(bm, dict) and bm.get("success"):
        return bm.get("meet_link") or bm.get("event_link")
    return None


async def handle_email_reply(lead_id: int, incoming_text: str, subject_in: str = "",
                             in_reply_to: str = "") -> None:
    """Corre el Agente 4 sobre la respuesta del prospecto y CONTESTA por correo."""
    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        if lead is None or not lead.email:
            return
        original_email = lead.email  # el buzón que respondió (puede ser general)
        company = lead.company_name or ""
        key = lead.email

    # Detectar rechazos explícitos (ej. "no gracias", "no nos interesa")
    text_lower = incoming_text.lower()
    negative_phrases = ["no gracias", "gracias no", "no me interesa", "no nos interesa", "no estamos interesados", "favor de remover", "cancelar"]
    if any(phrase in text_lower for phrase in negative_phrases):
        logger.info("Lead %s respondió rechazando la oferta: '%s'. Moviendo a DISCARDED.", lead_id, incoming_text)
        with session_scope() as db:
            lead = crud.get_lead_by_id(db, lead_id)
            if lead:
                crud.transition_lead_status(
                    db, lead, LeadStatus.DISCARDED, force=True,
                    reason=f"El prospecto declinó por correo: '{incoming_text[:50]}...'",
                    actor="email_reply_worker"
                )
        return

    runner = _get_chat_runner()
    prompt = (
        f"lead_id de esta conversación: {lead_id}\n"
        f"El prospecto ({company}) respondió a nuestro correo:\n\n"
        f"\"{incoming_text}\"\n\n"
        "Si te refieren a otra persona con un nuevo correo, usa redirect_conversation "
        "y luego redacta un correo NUEVO para esa persona. Si no, redacta tu respuesta. "
        "Devuelve SOLO el cuerpo del correo."
    )
    try:
        reply, tool_results = await _run_and_collect(
            runner, user_id=key, session_id=f"lead-{lead_id}", prompt=prompt
        )
    except Exception:
        logger.exception("Agente 4 falló procesando el lead %s.", lead_id)
        return

    if not reply:
        logger.info("Agente 4 no generó respuesta para el lead %s.", lead_id)
        return

    meet_link = _meet_link_from_results(tool_results)
    if meet_link and meet_link not in reply:
        reply += f"\n\nLink de la reunión (Google Meet): {meet_link}"

    # ¿Hubo REDIRECCIÓN? El correo del lead en la BD es la fuente de verdad:
    # si cambió, el Agente 4 llamó a redirect_conversation y el mensaje redactado
    # es para el NUEVO contacto -> se envía a la nueva dirección, en hilo NUEVO.
    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        current_email = lead.email if lead else original_email
    redirected = bool(current_email) and current_email != original_email

    if redirected:
        to_email = current_email
        subject = f"Propuesta de ahorro en CFE — {company}" if company else "Propuesta de ahorro en CFE — Jofra"
        reply_to = None  # correo NUEVO al contacto referido, no un reply al buzón general
        logger.info("Redirección detectada en lead %s: enviando a %s (nuevo hilo).", lead_id, to_email)
    else:
        to_email = original_email
        subject = f"Re: {subject_in}" if subject_in else "Re: su consulta — Jofra"
        reply_to = in_reply_to or None

    result = await send_email(to_email, subject, reply, reply_to=reply_to)

    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        if lead is None:
            return
        crud.save_chat_message(
            db, lead_id=lead_id, direction=MessageDirection.OUTBOUND,
            sender_type=SenderType.BOT, body=f"[Asunto: {subject}]\n{reply}",
            email_message_id=result.get("message_id"), message_type="email",
        )
        if meet_link:
            crud.mark_meeting_scheduled(db, lead)
            if lead.status != LeadStatus.MEETING_SCHEDULED:
                try:
                    crud.transition_lead_status(
                        db, lead, LeadStatus.MEETING_SCHEDULED,
                        reason="Reunión agendada por correo", actor="chat_manager",
                    )
                except crud.InvalidStateTransition:
                    crud.transition_lead_status(
                        db, lead, LeadStatus.MEETING_SCHEDULED, force=True,
                        reason="Reunión agendada (forzado)", actor="chat_manager",
                    )

        logger.info("Agente 4 respondió al lead %s%s (simulado=%s).",
            lead_id, " + REUNIÓN AGENDADA" if meet_link else "", result.get("simulated"))


# ======================================================================
# Envio del cold email del Agente 3 + registro
# ======================================================================

async def send_outreach_email(lead_id: int, subject: str, body: str) -> dict:
    """Envía el cold email y lo registra como mensaje del bot."""
    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        if lead is None or not lead.email:
            return {"sent": False, "error": "Lead sin correo."}
        to_email = lead.email

    result = await send_email(to_email, subject, body)

    with session_scope() as db:
        lead = crud.get_lead_by_id(db, lead_id)
        if lead is None:
            return result
        crud.save_chat_message(
            db, lead_id=lead_id, direction=MessageDirection.OUTBOUND,
            sender_type=SenderType.BOT, body=f"[Asunto: {subject}]\n{body}",
            email_message_id=result.get("message_id"), message_type="email",
        )
        if result.get("sent"):
            crud.mark_email_sent(db, lead)
    return result
