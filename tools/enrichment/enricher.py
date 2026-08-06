import logging
import pandas as pd
from pathlib import Path
from typing import List

from tools.apollo.apollo_client import search_leads, export_to_csv
from llm.model_factory import _qualified_model_name
from config.settings import get_settings
from litellm import completion
from database import crud
from database.db import session_scope
from database.models import LeadStatus

logger = logging.getLogger(__name__)

NORMALIZED_PROBABILITY_MAP = {
    "muy alta": "Muy Alta",
    "very high": "Muy Alta",
    "alta": "Alta",
    "high": "Alta",
    "media": "Media",
    "medium": "Media",
    "baja": "Baja",
    "low": "Baja",
    "muy baja": "Muy Baja",
    "very low": "Muy Baja",
}

def normalize_purchase_probability(raw_prob: str) -> str:
    """Normaliza la probabilidad de compra a 'High', 'Medium' o 'Low'."""
    cleaned = (raw_prob or "").strip().lower()
    return NORMALIZED_PROBABILITY_MAP.get(cleaned, "Medium")

def evaluate_lead_profile(
    company: str,
    role: str,
    sector: str,
    email: str,
    location: str = ""
) -> tuple[str, LeadStatus, str | None]:
    """Evalúa el perfil del prospecto aplicando las reglas de estrategia RAG de Jofra.

    Returns:
        (probability: "Muy Alta"|"Alta"|"Media"|"Baja"|"Muy Baja", status: LeadStatus, red_flag: str|None)
    """
    comp_lower = (company or "").lower()
    role_lower = (role or "").lower()
    sector_lower = (sector or "").lower()
    email_lower = (email or "").lower()
    loc_lower = (location or "").lower()

    email_prefix = email_lower.split('@')[0] if '@' in email_lower else ""
    role_or_email = f"{role_lower} {email_prefix}".strip()

    # Red Flag Check 0: Prospecto incompleto o malformado
    if not comp_lower and not role_lower and not sector_lower:
        return "Low", LeadStatus.DISCARDED, "Red Flag: Missing or incomplete prospect information"

    # Red Flag Check 1: Coworking / Plaza Comercial / Infraestructura no industrial
    coworking_keywords = ["coworking", "urban space", "shared office", "co-working"]
    non_industrial_keywords = ["plaza comercial", "plaza", "commercial suite"]

    if any(kw in comp_lower or kw in sector_lower or kw in loc_lower for kw in coworking_keywords):
        return "Low", LeadStatus.DISCARDED, "Red Flag: Coworking / Non-industrial facility roof ownership"

    if any(kw in comp_lower or kw in sector_lower or kw in loc_lower for kw in non_industrial_keywords):
        return "Low", LeadStatus.DISCARDED, "Red Flag: Non-industrial facility roof ownership"

    # Red Flag Check 2: Dominio de correo personal gratuito
    free_domains = ["gmail.com", "hotmail.com", "yahoo.com", "outlook.com"]
    if any(email_lower.endswith(f"@{domain}") for domain in free_domains):
        return "Low", LeadStatus.DISCARDED, "Red Flag: Free personal email domain"

    # Red Flag Check 3: Puesto no operacional o de bajo nivel
    non_operational_titles = ["community manager", "practicante", "becario", "auxiliar administrativo"]
    if role_lower and any(title in role_lower for title in non_operational_titles):
        return "Low", LeadStatus.DISCARDED, "Red Flag: Non-operational / low-level contact role"

    high_sectors = [
        "alimentos", "lácteos", "food & beverage", "frigorífico", "cold storage",
        "manufactura", "manufacturing", "plásticos", "empaques", "industrial", "solar", "energy"
    ]
    high_roles = [
        "director", "vp", "vicepresidente", "vice-presidente", "vice presidente",
        "gerente general", "cfo", "ceo", "coo", "chief operating officer",
        "head of operations", "presidente", "ejecutivo"
    ]

    has_role = bool(role_lower.strip())
    is_high_sector = any(s in sector_lower or s in comp_lower for s in high_sectors)
    is_high_role = any(r in role_or_email for r in high_roles)

    if is_high_sector and (is_high_role or not has_role):
        return "Muy Alta", LeadStatus.READY_FOR_OUTREACH, None

    medium_sectors = ["logística", "almacenes", "warehouse", "transporte", "distribución", "industrial"]
    medium_roles = ["supervisor", "jefe", "encargado", "ingeniero", "engineer", "proyectos", "gerente", "facilities", "lead", "coordinator"]

    if is_high_role or is_high_sector or any(s in sector_lower or s in comp_lower for s in medium_sectors):
        if is_high_role or not has_role or any(r in role_or_email for r in medium_roles):
            return "Alta", LeadStatus.READY_FOR_OUTREACH, None
            
    # Default if passed basic industrial filters but not explicitly high/medium
    return "Media", LeadStatus.READY_FOR_OUTREACH, None

    return "Muy Baja", LeadStatus.DISCARDED, "Low industrial fit"

def load_market_strategy_context() -> str:
    """Carga las reglas de estrategia de mercado RAG desde jofra_market_strategy.md."""
    strategy_path = Path.cwd() / "rag" / "jofra_market_strategy.md"
    if strategy_path.exists():
        try:
            return strategy_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Error leyendo jofra_market_strategy.md: {e}")
    return ""

async def generate_apollo_csv(
    domains: str = "",
    locations: str = "Mexico",
    output_filename: str = "apollo_leads.csv"
) -> str:
    """Busca leads en Apollo.io y los exporta a CSV.
    
    Args:
        domains: Dominios de empresas objetivo separados por salto de línea.
        locations: Ubicaciones (ej. "Mexico", "Monterrey").
        output_filename: Nombre del archivo CSV.
        
    Returns:
        Ruta absoluta del archivo CSV generado.
    """
    import re
    loc_list = [l.strip() for l in re.split(r'[,\n]', locations) if l.strip()]
    
    data = await search_leads(
        q_organization_domains=domains,
        locations=loc_list
    )
    
    people = data.get("contacts", []) if "contacts" in data else data.get("people", [])
    
    out_path = str(Path.cwd() / output_filename)
    export_to_csv(people, out_path)
    
    return out_path

async def enrich_csv_and_save_to_db(csv_path: str) -> str:
    """Lee un CSV crudo, filtra la basura con RAG y reglas de mercado, genera contexto de ventas y guarda en la BD.
    
    Por cada lead guardado con éxito y calificado para outreach, lo encola automáticamente.
    
    Args:
        csv_path: Ruta del CSV generado por Apollo.
        
    Returns:
        Resumen de la operación.
    """
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        return f"Error leyendo CSV: {e}"
        
    if df.empty:
        return "El CSV está vacío."
        
    saved_count = 0
    enqueued_count = 0
    leads_to_process = []
    settings = get_settings()
    model_name = _qualified_model_name(settings.agent2_qualifier_model)

    strategy_context = load_market_strategy_context()
    try:
        from tools.enrichment.rag import get_successful_pitches
        pitches_context = get_successful_pitches(k=3)
    except Exception:
        pitches_context = ""
    
    with session_scope() as db:
        for _, row in df.iterrows():
            email = str(row.get("Email", ""))
            title = str(row.get("Title", ""))
            company = str(row.get("Company", ""))
            sector = str(row.get("Industry", row.get("Sector", "")))
            if sector.strip().lower() in ["nan", "n/a", "none", ""]:
                sector = ""
            city = str(row.get("City", row.get("Location", "")))
            domain = str(row.get("Domain", ""))
            
            if not email or "@" not in email:
                continue  # Saltar sin correo
                
            # Evitar duplicados antes de gastar tokens
            if crud.get_lead_by_email(db, email):
                continue
            
            first_name = str(row.get('First Name', ''))
            last_name = str(row.get('Last Name', ''))
            contact_name = f"{first_name} {last_name}".strip()

            # Evaluación previa con reglas RAG de mercado
            rule_prob, rule_status, red_flag = evaluate_lead_profile(
                company=company,
                role=title,
                sector=sector,
                email=email,
                location=city
            )

            context = "He notado el crecimiento industrial de tu empresa."
            probability = rule_prob

            if rule_status == LeadStatus.READY_FOR_OUTREACH:
                prompt = f"""
                Eres Francisco Cantú, experto en ventas B2B de paneles solares industriales para Jofra en México.
                
                REGLAS DE ESTRATEGIA Y CONTEXTO RAG DE MERCADO:
                {strategy_context}
                
                EJEMPLOS DE PITCHES EXITOSOS:
                {pitches_context}
                
                PROSPECTO A EVALUAR:
                Nombre: {contact_name}
                Puesto: {title}
                Empresa: {company}
                Sector/Industria (Original): {sector if sector else "No especificado - debes inferirlo basado en la empresa"}
                Ubicación: {city}
                Correo: {email}
                
                Realiza tres tareas:
                1. Redacta UNA SOLA ORACIÓN muy natural, CREATIVA, hiper-personalizada y conversacional que sirva como rompehielos (icebreaker) o contexto para un correo frío. Trata a cada cliente como único. Menciona sutilmente por qué su empresa (por nombre) o su rol industrial hace sentido para reducir costos de energía con paneles solares de gran escala según el dolor del puesto. ¡NO repitas la misma frase para todos, sé original! No saludes.
                2. Califica la PROBABILIDAD DE COMPRA en base al puesto y la empresa usando estos 5 niveles exactos: (Muy Alta, Alta, Media, Baja, Muy Baja). Sé crítico, no agrupes a todos en Alta o Media, distribúyelos según su perfil real y tamaño de empresa.
                3. Infiere o mejora el SECTOR y la CIUDAD de la empresa. NUNCA respondas "N/A", "Desconocido", ni lo dejes en blanco. Si no viene el sector, usa "Industrial" o "Manufactura". Si no viene la ciudad, deduce su ubicación corporativa por el nombre o usa "México".

                FORMATO DE SALIDA ESTRICTO (cuatro líneas):
                ICEBREAKER: <oracion>
                PROBABILIDAD: <Muy Alta/Alta/Media/Baja/Muy Baja>
                SECTOR: <Sector deducido o confirmado, nunca N/A>
                CIUDAD: <Ciudad deducida o confirmada, nunca N/A>
                """
                try:
                    resp = completion(
                        model=model_name,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.7,
                        api_base=settings.ollama_api_base,
                        api_key=settings.ollama_api_key,
                    )
                    text = resp.choices[0].message.content.strip()
                    
                    for line in text.split('\n'):
                        if line.startswith("ICEBREAKER:"):
                            context = line.replace("ICEBREAKER:", "").strip()
                        elif line.startswith("PROBABILIDAD:"):
                            raw_prob = line.replace("PROBABILIDAD:", "").strip()
                            probability = normalize_purchase_probability(raw_prob)
                        elif line.startswith("SECTOR:"):
                            llm_sector = line.replace("SECTOR:", "").strip()
                            if llm_sector and llm_sector.lower() not in ["n/a", "nan", "none", "desconocido"]:
                                sector = llm_sector
                        elif line.startswith("CIUDAD:"):
                            llm_city = line.replace("CIUDAD:", "").strip()
                            if llm_city and llm_city.lower() not in ["n/a", "nan", "none", "desconocido"]:
                                city = llm_city
                except Exception as e:
                    logger.error(f"Error generando contexto para {email}: {e}")
                    probability = rule_prob
                    if not sector:
                        sector = "Industrial"
                    if not city:
                        city = "México"
            else:
                context = "Perfil no apto para prospección industrial."
                probability = "Muy Baja"

            lead = crud.create_lead(
                db,
                company_name=company,
                contact_name=contact_name,
                contact_role=title,
                email=email,
                website=domain,
                sector=sector,
                city=city,
                source="apollo",
                icebreaker_context=context,
                purchase_probability=probability,
            )
            if red_flag:
                lead.red_flags = red_flag

            crud.transition_lead_status(
                db, lead, rule_status,
                reason=f"Enriquecido desde Apollo CSV (Regla: {rule_status.value})", actor="enricher"
            )
            saved_count += 1
            if rule_status == LeadStatus.READY_FOR_OUTREACH:
                leads_to_process.append(lead.id)
            
    # === AUTO-ENCOLAMIENTO Y DRAFTING ===
    from orchestration.outreach_worker import schedule_lead_outreach
    from agents.outreach.agent import generate_outreach_email
    
    for lead_id in leads_to_process:
        try:
            with session_scope() as inner_db:
                lead_obj = crud.get_lead_by_id(inner_db, lead_id)
                if lead_obj:
                    # Generar el draft de inmediato para que se vea en el Kanban
                    subject, body = await generate_outreach_email(lead_obj)
                    crud.set_draft_email(inner_db, lead_obj, subject, body)
                    
            schedule_lead_outreach(lead_id)
            enqueued_count += 1
        except Exception as e:
            logger.error(f"Error encolando lead {lead_id} para outreach: {e}")
                
    return (
        f"Se enriquecieron y guardaron {saved_count} prospectos en la base de datos. "
        f"{enqueued_count} fueron encolados y se les redactó su correo automáticamente."
    )

