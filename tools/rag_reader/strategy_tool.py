import os
from pathlib import Path

def consultar_estrategia_jofra() -> str:
    """Lee el documento estratégico RAG (Base de Conocimiento de Jofra) y devuelve su contenido.
    
    Esta herramienta DEBE usarse siempre que el agente no sepa qué nicho, estado o ángulo de venta usar
    para generar leads. Le dice al agente exactamente qué empresas buscar y cómo contactarlas.
    
    Returns:
        Un string con toda la estrategia de ventas B2B, tarifas CFE, y diferenciadores.
    """
    rag_path = Path.cwd() / "rag" / "jofra_market_strategy.md"
    learned_path = Path.cwd() / "knowledge" / "learned_strategy.md"
    
    content = ""
    if rag_path.exists():
        with open(rag_path, "r", encoding="utf-8") as f:
            content += "=== ESTRATEGIA BASE (INTOCABLE) ===\n" + f.read() + "\n\n"
    else:
        content += "ERROR: No se encontró la estrategia base.\n"
        
    if learned_path.exists():
        with open(learned_path, "r", encoding="utf-8") as f:
            content += "=== ESTRATEGIA APRENDIDA (SELF-REFINING RAG) ===\n" + f.read()
            
    return content
