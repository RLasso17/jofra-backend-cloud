import asyncio
import logging
from tools.rag_reader.strategy_tool import consultar_estrategia_jofra
from agents.coordinator.agent import root_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_daily_autonomous_cycle():
    """Ejecuta el ciclo diario de prospección autónoma basado en el RAG estratégico."""
    logger.info("=== INICIANDO CICLO AUTÓNOMO DIARIO DE JOFRA ===")
    
    # 1. Le pasamos el RAG al agente root en el prompt y le pedimos que genere su ataque
    strategy_content = consultar_estrategia_jofra()
    
    # Instrucción para que el Coordinator actúe
    # Le limitamos el tamaño para no acabar los tokens ni los leads gratuitos de Apollo.
    prompt = f"""
    Eres el Coordinador Autónomo. Esta es tu Estrategia Comercial (RAG):
    {strategy_content}
    
    INSTRUCCIONES PARA HOY:
    1. Basado en el RAG, escoge un nicho específico y un estado para prospectar hoy (ej. "Data centers en Nuevo Leon").
    2. Usa `generate_apollo_csv` para buscar esos leads.
       IMPORTANTE: Usa parámetros para extraer a lo mucho 40-50 contactos hoy, para asegurar que nos mantenemos 
       debajo del límite mensual de API (Break-even point).
    3. Una vez extraídos, usa `transfer_to_agent` para pasar el CSV al Enricher para que califique los leads.
       Recuerda: LA META ES GENERAR VALOR PARA CONSEGUIR LA CITA, no vender.
       
    Ejecuta el pipeline AHORA.
    """
    
    try:
        # LLM decide la estrategia del dia
        from litellm import completion
        from config.settings import get_settings
        import json
        import os
        
        settings = get_settings()
        
        history_file = "knowledge/strategy_history.json"
        history = []
        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                pass
        
        history_text = "Historial reciente de prospección (NO REPETIR ESTOS SECTORES NI ESTAS CIUDADES):\n"
        if history:
            for item in history[-10:]: # Ultimos 10
                history_text += f"- Sector: {item.get('sector_name', '')} | Ciudad/Estado: {item.get('locations', '')}\n"
        else:
            history_text += "No hay historial previo.\n"
        
        system_prompt = f"""
        Eres el Orquestador Autonomo de Jofra. Lee esta estrategia:
        {strategy_content}
        
        {history_text}
        
        Tu tarea: Decide un sector y ubicación para prospectar hoy en base al RAG.
        Es OBLIGATORIO que elijas un sector y una ciudad completamente NUEVOS que no estén en el historial.
        Responde ÚNICAMENTE con un JSON válido con estas llaves:
        "sector_name" (ej. "Data Centers"), "locations" (ej. "Querétaro"), "domains" (déjalo vacío si vas por ubicación).
        """
        
        logger.info("Consultando al LLM para decidir el nicho del día...")
        resp = completion(
            model="gemma4:31b",
            messages=[{"role": "user", "content": system_prompt}],
            temperature=0.7,
            api_base=settings.ollama_api_base,
            api_key=settings.ollama_api_key,
        )
        
        text = resp.choices[0].message.content.strip()
        # Limpiar markdown de json si existe
        if text.startswith("```json"): text = text.replace("```json", "", 1)
        if text.endswith("```"): text = text[:text.rfind("```")]
        
        decision = json.loads(text.strip())
        sector_name = decision.get("sector_name", "Hoteles Históricos")
        locations = decision.get("locations", "Puebla, Oaxaca")
        domains = decision.get("domains", "")
        
        logger.info(f"Nicho seleccionado: {sector_name} en {locations}")
        
        # Guardar en historial
        history.append({"sector_name": sector_name, "locations": locations})
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        
        # Ejecutar Pipeline
        from tools.enrichment.enricher import generate_apollo_csv, enrich_csv_and_save_to_db
        csv_path = await generate_apollo_csv(domains=domains, locations=locations, output_filename="apollo_leads.csv", sector_name=sector_name)
        
        logger.info("Enriqueciendo prospectos...")
        res = await enrich_csv_and_save_to_db(csv_path)
        logger.info(f"Resultado final: {res}")
        
        # 5. Ejecutar la Supervisión y Auto-Aprendizaje (Self-Refining RAG) al final del día
        logger.info("Ejecutando Agente Supervisor para auto-aprendizaje...")
        from orchestration.daily_supervisor_trigger import run_daily_supervision
        await run_daily_supervision()
        
    except Exception as e:
        logger.error(f"Error en ciclo diario: {e}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(run_daily_autonomous_cycle())
