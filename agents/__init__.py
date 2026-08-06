# agents/__init__.py
"""Paquete de agentes ADK de Jofra. Expone el root_agent (Coordinador)."""

from agents.coordinator.agent import root_agent

__all__ = ["root_agent"]
