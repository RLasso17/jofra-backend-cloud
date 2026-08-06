# config/settings.py
"""
Configuracion central del sistema de prospeccion de Jofra.

Toda variable sensible vive en el archivo .env (raiz del proyecto) y se carga
aqui via pydantic-settings. Ningun otro modulo debe leer os.environ directo:
siempre importar `get_settings()` desde este archivo.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Raiz del proyecto: .../Claude4Jofra
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Los nombres de campo no usan prefijo "model_" para no chocar con el
        # namespace protegido de Pydantic v2.
    )

    # ------------------------------------------------------------------
    # OLLAMA / LITELLM (infraestructura LLM remota)
    # ------------------------------------------------------------------
    ollama_api_base: str = "https://ollama.com"
    ollama_api_key: str = ""

    # Asignacion de modelos por agente. LiteLLM requiere el prefijo de proveedor
    # "ollama_chat/" (lo agrega la factoria).
    #
    # REGLA DURA - TOOL-CALLING BAJO STREAMING: la UI de ADK (adk web) ejecuta en
    # modo SSE, y con Ollama el manejo de tool_calls fragmentados depende del
    # modelo. Los 4 agentes que usan herramientas DEBEN correr en un modelo
    # verificado bajo streaming. Medido empiricamente:
    #   PASAN: qwen3-coder-next, ministral-3:14b, ministral-3:8b, ministral-3:3b,
    #          devstral-small-2:24b.
    #   FALLAN (alucinan el JSON): gemma3 (cualquier tamaño), nemotron-3-nano,
    #          qwen3-next:80b, gpt-oss:20b.
    # Por eso gemma3 SOLO se usa en el Outreach (texto puro, sin tools).
    #
    # CASO ESPECIAL DEL COORDINADOR: ademas de hacer tool-calling, usa
    # transfer_to_agent, que rastrea IDs de tool-calls entre turnos en el flujo
    # multi-agente. devstral-small-2:24b crasheo aqui con "Unexpected tool call
    # id" al delegar; por eso el Coordinador se queda FIJO en qwen3-coder-next
    # (el unico 100% estable en delegacion). NO cambiar el modelo del coordinador
    # sin reverificar la delegacion.
    #
    # - Coordinator:  qwen3-coder-next — cerebro de ruteo + transfer estable.
    # - Lead Finder:  ministral-3:14b  — agente HOJA (function-tools, sin
    #     transfer), robusto y verificado bajo streaming; distinto y mas ligero
    #     que qwen. (No le afecta el bug de transfer del coordinador.)
    # - Qualifier:    ministral-3:3b   — logica simple (sector + WhatsApp valido);
    #     el verificado mas ligero, maximo ahorro de GPU.
    # - Chat Manager: ministral-3:8b   — balanceado: conversacion + book_meeting.
    # - Outreach:     gemma3:12b       — redaccion persuasiva pura, sin tools.
    agent0_coordinator_model: str = "gpt-oss:120b"   # tools + transfer — FIJO (estable)
    agent1_lead_finder_model: str = "gpt-oss:120b"   # function-tools (hoja) — robusto, distinto
    agent2_qualifier_model: str = "nemotron-3-nano:30b"       # tools — logica simple, ultraligero
    agent3_outreach_model: str = "nemotron-3-ultra"            # SOLO texto — persuasivo
    agent4_chat_manager_model: str = "gemma4:31b"    # tool book_meeting — balanceado



    # ------------------------------------------------------------------
    # APOLLO.IO API (Búsqueda B2B y extracción de leads)
    # ------------------------------------------------------------------
    apollo_api_key: str = ""
    apollo_max_results: int = 10
    
    @property
    def apollo_enabled(self) -> bool:
        return bool(self.apollo_api_key)

    # ------------------------------------------------------------------
    # GOOGLE SHEETS (sincronización en vivo del pipeline de leads)
    # ------------------------------------------------------------------
    # Service Account (JSON) con acceso al Sheet compartido. Comparte el Sheet
    # con el email del service account. Sin esto, la sync a Sheets se omite.
    google_sheets_credentials_file: str = "sheets_service_account.json"
    google_sheet_id: str = ""       # ID del Google Sheet (de su URL)
    google_sheet_tab: str = "Leads"

    @property
    def sheets_enabled(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # ESCALABILIDAD / MANEJO DE AVALANCHAS (Agentes 3 y 4)
    # ------------------------------------------------------------------
    # Máximo de llamadas al LLM concurrentes por worker (evita saturar Ollama).
    outreach_concurrency: int = 3
    reply_concurrency: int = 3
    reply_batch_size: int = 20      # respuestas por ciclo del lector IMAP

    # ------------------------------------------------------------------
    # COLD EMAIL (canal oficial de prospeccion) - SMTP (envio) + IMAP (lectura)
    # ------------------------------------------------------------------
    # Para GoDaddy: smtpout.secureserver.net:465 (SSL) o 587 (TLS), imap.secureserver.net:993
    smtp_host: str = "smtpout.secureserver.net"
    smtp_port: int = 587
    smtp_user: str = ""        # tu correo (ej. ventas@jofra.com)
    smtp_password: str = ""    # Contraseña de GoDaddy
    email_from: str = ""       # remitente; si vacio, se usa smtp_user
    email_from_name: str = "Francisco Cantú · Jofra"

    imap_host: str = "imap.secureserver.net"
    imap_port: int = 993

    @property
    def email_enabled(self) -> bool:
        """¿Hay credenciales para ENVIAR correos de verdad?"""
        return bool(self.smtp_user and self.smtp_password)

    @property
    def imap_enabled(self) -> bool:
        """¿Hay credenciales para LEER la bandeja de entrada?"""
        return bool(self.imap_host and self.smtp_user and self.smtp_password)

    @property
    def from_address(self) -> str:
        return self.email_from or self.smtp_user or "no-reply@example.com"

    # ------------------------------------------------------------------
    # GOOGLE CALENDAR (Fase 3)
    # ------------------------------------------------------------------
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"
    google_calendar_id: str = "primary"

    # ------------------------------------------------------------------
    # SERVIDOR
    # ------------------------------------------------------------------
    app_host: str = "0.0.0.0"
    app_port: int = 8001

    # ------------------------------------------------------------------
    # BASE DE DATOS
    # ------------------------------------------------------------------
    database_url: str = ""

    @property
    def effective_database_url(self) -> str:
        """URL final de SQLAlchemy. SQLite local por defecto.

        Se usa as_posix() porque las URLs de SQLAlchemy no aceptan
        backslashes de Windows.
        """
        if self.database_url:
            return self.database_url
        db_path = (BASE_DIR / "database" / "jofra.db").as_posix()
        return f"sqlite:///{db_path}"

    @property
    def adk_session_db_url(self) -> str:
        """URL para el DatabaseSessionService de ADK.

        ADK usa el motor ASINCRONO de SQLAlchemy, que en SQLite requiere el
        driver aiosqlite (el pysqlite sincrono no sirve). Apunta al MISMO
        archivo .db que el resto del sistema; las tablas de sesion de ADK
        conviven con las de negocio.
        """
        sync_url = self.effective_database_url
        if sync_url.startswith("sqlite:///"):
            return sync_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        return sync_url

    # ------------------------------------------------------------------
    # COLA DE OUTREACH (Agente 3) - retraso humano anti-ban
    # ------------------------------------------------------------------
    outreach_delay_min_seconds: int = 60
    outreach_delay_max_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    """Singleton de configuracion (cacheado para todo el proceso)."""
    return Settings()

def is_system_active() -> bool:
    import json
    import os
    state_file = BASE_DIR / "system_state.json"
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f).get("is_active", True)
        except Exception:
            pass
    return True

