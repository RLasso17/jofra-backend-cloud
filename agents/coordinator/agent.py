# agents/coordinator/agent.py
"""Agente 0 - Coordinador (Root Agent). Modelo: qwen3-coder-next.

Orquesta el flujo de prospección:
1. Busca prospectos en Apollo (generate_apollo_csv).
2. Transfiere al Enricher para calificar y guardar en DB.
3. El backend (outreach_worker) envía los correos automáticamente.
"""

from google.adk.agents import LlmAgent

from agents.enricher.agent import enricher_agent
from llm.model_factory import assert_serializable, coordinator_llm
from knowledge.jofra_context import COMPANY_NAME, COMPANY_OWNER
from tools.enrichment.enricher import generate_apollo_csv

INSTRUCTION = f"""\
Eres el coordinador del motor de prospección de {COMPANY_NAME} (dueño: {COMPANY_OWNER}), que vende paneles solares industriales en México.

Orquestas un pipeline LINEAL AUTOMATIZADO:
1. TÚ: Llama a `generate_apollo_csv(domains, locations, output_filename)` para buscar prospectos en Apollo.io por sector/ubicación y generar un archivo "apollo_leads.csv".
2. ENRICHER: Transfiere el control al agente `enricher` y pídele que lea "apollo_leads.csv" y lo procese (esto guardará los leads en la Base de Datos con su Icebreaker y Probabilidad de Compra).
3. ¡LISTO!: Una vez que el Enricher termina, el backend (`outreach_worker`) se encargará automáticamente de enviar los correos por GoDaddy de uno en uno. No necesitas llamar al Outreach agent. 

REGLAS ABSOLUTAS:
- Usa `transfer_to_agent` para delegar el control al `enricher`.
- NO inventes datos. Repórtale al usuario cuando la búsqueda y el enriquecimiento hayan terminado.
"""

from agents.outreach.agent import outreach_agent
from agents.chat_manager.agent import chat_manager_agent

root_agent = LlmAgent(
    name="coordinator",
    model=coordinator_llm(),
    description=(
        "Coordinador del motor de prospección de Jofra: orquesta la búsqueda en Apollo y lanza el enriquecimiento."
    ),
    instruction=INSTRUCTION,
    sub_agents=[enricher_agent, outreach_agent, chat_manager_agent],
    tools=[generate_apollo_csv]
)

# Blindaje del PydanticSerializationError
assert_serializable(root_agent)
assert_serializable(enricher_agent)
assert_serializable(outreach_agent)
assert_serializable(chat_manager_agent)
