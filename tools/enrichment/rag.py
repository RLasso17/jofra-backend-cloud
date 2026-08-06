import chromadb
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

_chroma_client = None

def get_chroma_collection():
    global _chroma_client
    if not _chroma_client:
        db_path = str(Path.cwd() / "chroma_db")
        _chroma_client = chromadb.PersistentClient(path=db_path)
    return _chroma_client.get_or_create_collection(name="successful_pitches")

def save_successful_pitch(lead_id: str, company: str, subject: str, body: str):
    """Guarda un correo exitoso en la base de datos vectorial para inspiracion futura."""
    try:
        col = get_chroma_collection()
        col.add(
            documents=[f"ASUNTO: {subject}\n\n{body}"],
            metadatas=[{"company": company, "lead_id": str(lead_id)}],
            ids=[f"lead_{lead_id}"]
        )
        logger.info(f"Guardado correo exitoso en RAG (lead {lead_id})")
    except Exception as e:
        logger.error(f"Error guardando en RAG: {e}")

def get_successful_pitches(k: int = 3) -> str:
    """Recupera ejemplos de correos exitosos recientes para inspirar al LLM."""
    try:
        col = get_chroma_collection()
        result = col.get(limit=k)
        docs = result.get('documents', [])
        if not docs:
            return ""
        return "\n---\n".join(docs)
    except Exception as e:
        logger.error(f"Error recuperando de RAG: {e}")
        return ""
