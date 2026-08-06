from database.db import session_scope
from database.models import OutboundQueueItem, Lead
import json

def get_latest_emails():
    with session_scope() as db:
        items = db.query(OutboundQueueItem).order_by(OutboundQueueItem.id.desc()).limit(10).all()
        results = []
        for item in items:
            lead = db.query(Lead).filter(Lead.id == item.lead_id).first()
            if lead:
                results.append({
                    "empresa": lead.company_name,
                    "contacto": lead.contact_name,
                    "cuerpo": item.body,
                    "estado": item.status.value
                })
        print(json.dumps(results, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    get_latest_emails()
