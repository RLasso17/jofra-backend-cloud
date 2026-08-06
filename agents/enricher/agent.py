# agents/enricher/agent.py
"""Agente Enriquecedor y Filtrador.

Toma el CSV crudo generado por Apollo, investiga a las empresas/prospectos, 
filtra los que no sirven, y genera un archivo Excel (.xlsx) de alta calidad
listo para ser procesado por el Agente de Outreach.
"""

from google.adk.agents import LlmAgent

from tools.enrichment.enricher import enrich_csv_and_save_to_db
from llm.model_factory import qualifier_llm

INSTRUCTION = """\
Eres el Agente Enriquecedor de Leads B2B.

Tu trabajo es procesar el archivo CSV generado por Apollo.io.
Usa la herramienta `enrich_csv_and_save_to_db` pasándole la ruta del CSV (ej. "apollo_leads.csv").
La herramienta se encargará de leerlo, generar un contexto de ventas rompehielos (icebreaker) para cada lead usando IA, filtrar la basura, y guardar los leads resultantes directamente en la base de datos (lo que a su vez los sincroniza con Google Sheets).
Una vez que tengas la confirmación de la herramienta de cuántos leads se guardaron, tu trabajo termina y debes reportárselo al Coordinador.
"""

enricher_agent = LlmAgent(
    name="enricher",
    model=qualifier_llm(),
    description="Procesa CSVs de Apollo y los guarda en la base de datos enriquecidos.",
    instruction=INSTRUCTION,
    tools=[enrich_csv_and_save_to_db],
)
