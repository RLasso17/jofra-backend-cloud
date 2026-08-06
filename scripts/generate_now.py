import asyncio
import logging
from config.settings import get_settings
from database.db import session_scope
from database.models import OutboundQueueItem, OutboundStatus, Lead
from database import crud
from orchestration.runners import generate_outreach_email

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def process_queue_test():
    with session_scope() as db:
        pending = db.query(OutboundQueueItem).filter(OutboundQueueItem.status == OutboundStatus.PENDING).all()
        logger.info(f"Procesando {len(pending)} correos...")
        
        for item in pending:
            lead = db.query(Lead).filter(Lead.id == item.lead_id).first()
            logger.info(f"Generando correo para: {lead.company_name}")
            
            # Generar email
            email = await generate_outreach_email(lead.id)
            if email and email.get("body"):
                body = f"ASUNTO: {email['subject']}\n\n{email['body']}"
                item.body = body
                item.status = OutboundStatus.SENT
                db.commit()
                print("--------------------------------------------------")
                print(f"Empresa: {lead.company_name}")
                print(f"Contacto: {lead.contact_name}")
                print(f"Correo generado:\n{body}")
                print("--------------------------------------------------")
                
if __name__ == "__main__":
    asyncio.run(process_queue_test())
