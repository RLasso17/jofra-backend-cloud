import logging
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

def search_company_info(company_name: str, domain: str = "") -> str:
    """Busca en la web de forma gratuita informacion sobre una empresa.
    
    Se utiliza DuckDuckGo para evitar costos de API de busqueda (como Tavily o Serper).
    """
    if not company_name:
        return "Sin informacion de empresa."
        
    query = f"{company_name} {domain} a que se dedica, historia, tamaño de empresa"
    logger.info(f"Deep Research (Gratis) buscando: {query}")
    
    try:
        results = DDGS().text(query, max_results=3)
        if not results:
            return "No se encontro informacion relevante en la web."
            
        snippets = []
        for r in results:
            snippets.append(r.get("body", ""))
            
        # Unimos los snippets en un solo texto de contexto
        return "\n".join(snippets)
        
    except Exception as e:
        logger.error(f"Error en Deep Research para {company_name}: {e}")
        return "Error al buscar informacion en la web."

if __name__ == "__main__":
    # Prueba rapida
    info = search_company_info("Ternium", "ternium.com")
    print(f"Info encontrada:\n{info}")
