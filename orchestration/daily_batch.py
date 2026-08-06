import logging
import asyncio
from typing import List, Dict

from tools.enrichment.enricher import generate_apollo_csv, enrich_csv_and_save_to_db

logger = logging.getLogger(__name__)

# Definimos los sectores y los dominios o keywords para buscar en Apollo
SECTORS_CONFIG = [
    {
        "name": "Manufactura",
        "domains": "ternium.com\ncemex.com\nfemsa.com", # Aqui irian más ejemplos o dejar vacio y usar keywords de industria en Apollo si estuviera implementado
        "locations": "Mexico"
    },
    {
        "name": "Hospitales",
        "domains": "hospitalangeles.com\nchristusmuguerza.com.mx\nstarmedica.com",
        "locations": "Mexico"
    },
    {
        "name": "Agroindustria",
        "domains": "suKarne.com\nbachoco.com.mx\ngrupobimbo.com",
        "locations": "Mexico"
    },
    {
        "name": "Hoteles",
        "domains": "posadas.com\nvidanta.com\npalaceresorts.com",
        "locations": "Mexico"
    }
]

async def run_daily_batch(max_total_leads: int = 150):
    """Ejecuta la busqueda diaria intercalando sectores hasta alcanzar el limite."""
    logger.info("Iniciando CRON Diario: Batch de %d leads", max_total_leads)
    
    # Para el MVP, simplemente tomamos los primeros 3 sectores en la lista 
    # y rotamos la lista para mañana (en un sistema robusto, guardariamos el indice en DB).
    # Por ahora, extraemos 1 pagina de cada sector hasta acabar o llegar a 150.
    
    import random
    sectors_to_run = random.sample(SECTORS_CONFIG, 3) # Tomamos 3 sectores al azar hoy
    
    total_processed = 0
    
    for sector in sectors_to_run:
        if total_processed >= max_total_leads:
            logger.info("Se alcanzo el limite diario de leads (%d).", max_total_leads)
            break
            
        logger.info(f"Procesando sector: {sector['name']}")
        
        try:
            csv_path = await generate_apollo_csv(
                domains=sector["domains"],
                locations=sector["locations"],
                output_filename=f"apollo_batch_{sector['name']}.csv"
            )
            
            result_msg = await enrich_csv_and_save_to_db(csv_path)
            logger.info(f"Resultado para {sector['name']}: {result_msg}")
            
            # Aqui no sabemos exactamente cuantos se guardaron sin parsear el result_msg,
            # pero asumiremos un estimado de 50 por pagina (por los rate limits de GoDaddy).
            total_processed += 50 
            
        except Exception as e:
            logger.error(f"Error procesando sector {sector['name']}: {e}")
            
    logger.info("Batch diario finalizado.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_daily_batch())
