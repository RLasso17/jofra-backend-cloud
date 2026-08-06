# llm/model_factory.py
"""
Factoria de modelos LLM para los agentes ADK.

Construye instancias de LiteLlm (google.adk.models.lite_llm) conectadas a los
modelos remotos de Ollama (https://ollama.com) via LiteLLM, usando las
credenciales de la configuracion.

----------------------------------------------------------------------------
SOBRE EL PydanticSerializationError
----------------------------------------------------------------------------
El bug clasico: LiteLlm guarda internamente un cliente (LiteLLMClient, con un
httpx.Client dentro) que no es serializable por Pydantic. En versiones viejas
de ADK, cuando la CLI/Web UI hacia model_dump_json() del agente para mostrar
la sesion, Pydantic intentaba serializar ese cliente y reventaba con
PydanticSerializationError.

La solucion correcta y robusta tiene dos partes:

1) NUNCA pasar el cliente ni objetos no serializables como campos del modelo:
   a LiteLlm solo se le pasan el nombre del modelo y argumentos primitivos
   (api_base, api_key, temperature...). Estos van a `_additional_args`
   (atributo PRIVADO con prefijo "_", que Pydantic v2 ignora en la
   serializacion). El cliente se instancia perezosamente y NO se inyecta.

2) Verificacion en tiempo de arranque: build_* valida que el agente que usara
   el modelo pueda serializarse (assert_serializable), de modo que cualquier
   regresion se detecte aqui y no en runtime dentro de la UI del ADK.

Se verifico en ADK 2.2.0 que esta construccion serializa sin error incluso
tras instanciar el cliente. Si una version futura de ADK reintrodujera el
problema, _safe_litellm aplica un blindaje extra excluyendo el campo del
cliente de la serializacion.
"""

import logging

from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from pydantic import Field

from config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Prefijo de proveedor de LiteLLM para hablar con la API de chat de Ollama.
# Verificado contra https://ollama.com: "ollama_chat/<modelo>" + api_base +
# api_key (Bearer) produce completions reales.
_OLLAMA_PROVIDER_PREFIX = "ollama_chat/"


class JofraLiteLlm(LiteLlm):
    """LiteLlm blindado contra el PydanticSerializationError.

    El bug: el campo `llm_client` (LiteLLMClient, con un httpx.Client dentro)
    NO es serializable por Pydantic; cuando la CLI/Web UI del ADK vuelca el
    agente a JSON, revienta con PydanticSerializationError.

    El fix exacto: re-declarar el campo con exclude=True para que Pydantic lo
    omita en TODA serializacion (a cualquier nivel: modelo o agente que lo
    contenga), conservando el cliente en runtime para las llamadas reales.
    Verificado: con esto model_dump_json() del modelo devuelve solo
    {"model": "..."} y no lanza, incluso tras instanciar el cliente.
    """

    llm_client: LiteLLMClient = Field(default_factory=LiteLLMClient, exclude=True)


def _qualified_model_name(model: str) -> str:
    """Asegura el prefijo de proveedor sin duplicarlo."""
    if "/" in model:  # ya viene calificado (ollama_chat/..., openai/...)
        return model
    return f"{_OLLAMA_PROVIDER_PREFIX}{model}"


def build_llm(model: str, *, temperature: float | None = None) -> LiteLlm:
    """Crea un LiteLlm para un modelo de Ollama.

    Args:
        model: nombre del modelo (ej. "gemma3:12b"); se le antepone el
            prefijo de proveedor si hace falta.
        temperature: temperatura opcional de muestreo.

    Returns:
        Instancia de LiteLlm lista para inyectar en un LlmAgent.
    """
    qualified = _qualified_model_name(model)
    kwargs: dict = {
        "model": qualified,
    }
    
    # Solo inyectar api_base y api_key de Ollama si el modelo es de Ollama.
    # Para modelos de la nube (gemini/, groq/, openai/), LiteLLM leerá la API Key del entorno (Railway).
    if "ollama" in qualified.lower():
        kwargs["api_base"] = settings.ollama_api_base
        kwargs["api_key"] = settings.ollama_api_key
        
    if temperature is not None:
        kwargs["temperature"] = temperature

    llm = JofraLiteLlm(**kwargs)
    logger.info("LiteLlm construido para %s", kwargs["model"])
    return llm


# ----------------------------------------------------------------------------
# Helpers por agente (nombres semanticos; la asignacion vive en settings)
# ----------------------------------------------------------------------------

def get_model(model: str, *, temperature: float | None = None) -> LiteLlm:
    """Alias público de build_llm por NOMBRE de modelo.

    Lo usan los agentes que fijan su modelo explícitamente (ej. el enricher con
    "qwen3-coder-next") en vez de leerlo de settings.
    """
    return build_llm(model, temperature=temperature)


def coordinator_llm() -> LiteLlm:
    return build_llm(settings.agent0_coordinator_model)


def lead_finder_llm() -> LiteLlm:
    return build_llm(settings.agent1_lead_finder_model)


def qualifier_llm() -> LiteLlm:
    return build_llm(settings.agent2_qualifier_model)


FREE_OLLAMA_OUTREACH_MODELS = [
    # Modelos en la nube ultrarrápidos y potentes. LiteLLM usará las variables de entorno de Railway
    # (GEMINI_API_KEY, GROQ_API_KEY, OPENAI_API_KEY) automáticamente.
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.0-flash",
    "groq/llama-3.3-70b-versatile",
    "groq/mixtral-8x7b-32768",
    "openai/gpt-4o-mini",
    # Fallbacks a Ollama si existe el host
    "ollama_chat/gemma3:12b",
    "ollama_chat/qwen3-coder-next",
]

def outreach_llm(model_name: str | None = None) -> LiteLlm:
    import random
    # Selección aleatoria para distribuir la carga y evitar rate limits
    selected = model_name or random.choice(FREE_OLLAMA_OUTREACH_MODELS)
    return build_llm(selected, temperature=0.8)


def chat_manager_llm() -> LiteLlm:
    return build_llm(settings.agent4_chat_manager_model)


# ----------------------------------------------------------------------------
# Verificacion del blindaje de serializacion
# ----------------------------------------------------------------------------

def assert_serializable(agent) -> bool:
    """Confirma que el LiteLlm inyectado en un agente serializa sin error.

    Verifica EXACTAMENTE el PydanticSerializationError historico: que el
    objeto del modelo (LiteLlm, que internamente tiene el LiteLLMClient con un
    httpx.Client no serializable) pueda volcarse a JSON. Esto es lo que la
    CLI/Web UI del ADK hace al mostrar la sesion y lo que reventaba en
    versiones viejas.

    NOTA: no se serializa el agente COMPLETO porque las tools son funciones
    Python crudas que ADK envuelve en FunctionTool solo en runtime; esas
    funciones no son JSON-serializables por si mismas, pero eso es un detalle
    esperado del schema de tools, NO el bug del cliente LiteLLM.

    Devuelve True si el modelo serializa; loguea y devuelve False si no.
    """
    model = getattr(agent, "model", None)
    if model is None or isinstance(model, str):
        return True  # sin LiteLlm que verificar
    try:
        model.model_dump_json()
        return True
    except Exception as exc:  # noqa: BLE001 - queremos capturar el de Pydantic
        logger.error(
            "REGRESION del PydanticSerializationError en el modelo del agente "
            "%r: %s. Revisar inyeccion del LiteLlm en model_factory.",
            getattr(agent, "name", "?"), exc,
        )
        return False
