import csv
import logging
from pathlib import Path
from typing import Any, Dict, List

import httpx

from config.settings import get_settings

logger = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/v1"

async def search_leads(
    q_organization_domains: str = "",
    person_titles: List[str] = None,
    contact_email_status: List[str] = None,
    locations: List[str] = None,
    page: int = 1,
) -> Dict[str, Any]:
    """Busca personas en Apollo.io.
    
    Args:
        q_organization_domains: Dominios separados por salto de línea (ej. "jofra.com\nacme.com")
        person_titles: Puestos (ej. ["ceo", "director"])
        contact_email_status: Filtro de estado del correo (ej. ["verified"])
        locations: Ubicaciones (ej. ["Mexico", "Monterrey"])
        page: Número de página.
        
    Returns:
        Dict con los resultados de la API.
    """
    settings = get_settings()
    if not settings.apollo_enabled:
        raise ValueError("APOLLO_API_KEY no está configurada en el .env.")

    if person_titles is None:
        person_titles = ["ceo", "director", "manager", "owner", "gerente", "dueño"]
    if contact_email_status is None:
        contact_email_status = ["verified"]
    if locations is None:
        locations = ["Mexico"]

    url = f"{APOLLO_BASE_URL}/mixed_people/api_search"
    
    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": settings.apollo_api_key,
    }
    
    payload = {
        "q_organization_domains": q_organization_domains,
        "person_titles": person_titles,
        "contact_email_status": contact_email_status,
        "person_locations": locations,
        "page": page,
        "per_page": min(settings.apollo_max_results, 100) # Límite por petición
    }

    async with httpx.AsyncClient() as client:
        # Paso 1: Buscar a las personas (esto nos da IDs pero no correos)
        response = await client.post(url, headers=headers, json=payload, timeout=30.0)
        response.raise_for_status()
        search_data = response.json()
        people_found = search_data.get("people", [])
        
        if not people_found:
            return search_data
            
        logger.info(f"Paso 1 completado: Se encontraron {len(people_found)} IDs. Iniciando desbloqueo (Paso 2)...")
        
        # Paso 2: Desbloquear los correos reales uno por uno
        enriched_people = []
        import asyncio
        for p in people_found:
            person_id = p.get("id")
            if not person_id:
                continue
                
            match_url = f"{APOLLO_BASE_URL}/people/match"
            match_payload = {"id": person_id}
            
            try:
                # Retardo de 0.5s para no saturar los Rate Limits de Apollo
                await asyncio.sleep(0.5)
                match_response = await client.post(match_url, headers=headers, json=match_payload, timeout=30.0)
                if match_response.status_code == 200:
                    match_data = match_response.json()
                    enriched_person = match_data.get("person", p)
                    enriched_people.append(enriched_person)
                    logger.debug(f"Desbloqueado correo para {person_id}")
                else:
                    logger.warning(f"Error {match_response.status_code} al desbloquear ID {person_id}")
                    enriched_people.append(p) # Guardamos el original aunque no tenga correo
            except Exception as e:
                logger.error(f"Error al desbloquear {person_id}: {e}")
                enriched_people.append(p)
                
        # Reemplazamos la lista original con los perfiles enriquecidos
        search_data["people"] = enriched_people
        return search_data

def export_to_csv(people: List[Dict[str, Any]], output_path: str) -> str:
    """Exporta los resultados de Apollo a un archivo CSV.
    
    Extrae Nombre, Apellido, Puesto, Correo, Empresa, Dominio, LinkedIn y Ciudad.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    headers = [
        "First Name", "Last Name", "Title", "Email", 
        "Company", "Domain", "LinkedIn", "City"
    ]
    
    with open(path, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        
        for p in people:
            org = p.get("organization") or {}
            writer.writerow({
                "First Name": p.get("first_name", ""),
                "Last Name": p.get("last_name", ""),
                "Title": p.get("title", ""),
                "Email": p.get("email", ""),
                "Company": org.get("name", ""),
                "Domain": org.get("website_url", ""),
                "LinkedIn": p.get("linkedin_url", ""),
                "City": p.get("city", "")
            })
            
    logger.info("Exportados %d leads a %s", len(people), output_path)
    return str(path)
