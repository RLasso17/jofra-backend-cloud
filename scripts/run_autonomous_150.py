import asyncio
import logging
from database.db import engine, Base, session_scope
from database.models import Lead, LeadStatus
from sqlalchemy import select, func
from tools.apollo.apollo_client import search_leads, export_to_csv
from tools.enrichment.enricher import enrich_csv_and_save_to_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_autonomous_150():
    print("Wiping database to start fresh (150 leads target)...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    target_leads = 150
    page = 1
    
    while True:
        with session_scope() as db:
            current_count = db.scalar(select(func.count(Lead.id)).where(Lead.status == LeadStatus.READY_FOR_OUTREACH)) or 0
            
        if current_count >= target_leads:
            print(f"Target reached! We have {current_count} unique leads ready for outreach.")
            break
            
        print(f"Current leads: {current_count}/{target_leads}. Searching Apollo (Page {page})...")
        
        try:
            # Query Apollo API for people
            data = await search_leads(
                person_titles=["ceo", "director", "gerente", "vp", "dueño"],
                locations=["Mexico", "Monterrey", "Guadalajara", "Queretaro"],
                page=page
            )
            
            people = data.get("contacts", []) if "contacts" in data else data.get("people", [])
            if not people:
                print("No more people found on Apollo. Exiting loop.")
                break
                
            csv_path = f"apollo_batch_autonomous_page_{page}.csv"
            export_to_csv(people, csv_path)
            
            # Enrich, de-duplicate, and save to DB
            result_msg = await enrich_csv_and_save_to_db(csv_path)
            print(result_msg)
            
            page += 1
            
        except Exception as e:
            print(f"Error during search/enrich cycle: {e}")
            break

    from export_to_excel import export_leads_to_excel
    export_leads_to_excel()
    print("Autonomous cycle complete. Excel synced.")

if __name__ == "__main__":
    asyncio.run(run_autonomous_150())
