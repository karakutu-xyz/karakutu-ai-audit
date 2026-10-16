"""Clients for external services this audit service consumes.

Currently just the Karakutu Server API (the archive's own backend), which supplies the
article store, server-side similarity (step 2) and the IPTC concept vocabulary (step 1).
"""
from src.clients.concepts import Concept, ConceptCatalog, load_catalog
from src.clients.karakutu import KarakutuClient, get_client
from src.clients.models import (
    ArticleRecord,
    NewspaperRef,
    SimilarArticle,
    SimilarArticlesResult,
)

__all__ = [
    "Concept",
    "ConceptCatalog",
    "load_catalog",
    "KarakutuClient",
    "get_client",
    "ArticleRecord",
    "NewspaperRef",
    "SimilarArticle",
    "SimilarArticlesResult",
]
