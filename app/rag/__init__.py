"""Knowledge/RAG retrieval infrastructure (Phase 3).

This package implements the deterministic retrieval pipeline that will back
the future Knowledge/RAG Agent: parsing policy documents, chunking them,
embedding, persisting to ChromaDB, and ranking retrieval results. It contains
no LLM calls, no planning, and no final-answer generation -- see
app/services/knowledge.py for the service boundary that converts retrieval
results into the existing app.schemas.evidence.Evidence contract.
"""
