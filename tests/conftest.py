from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.app import app, lifespan


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    # ASGITransport in httpx 0.28 does not run the lifespan; do it ourselves so
    # that app.state.{relevance,similarity,web_search}_agent are populated.
    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def valid_audit_request() -> dict:
    removed = {"url": "https://example.com/removed", "title": "Removed", "content": "Some content."}
    similars = [
        {"url": f"https://example.com/s{i}", "title": f"Similar {i}", "content": f"Content {i}."}
        for i in range(5)
    ]
    return {"removed_article": removed, "similar_articles": similars}


@pytest.fixture(autouse=True)
def _no_network_catalog(monkeypatch):
    """The lifespan pulls the IPTC vocabulary from the archive; tests must never open a
    socket, so the loader is replaced with "no catalog" (classification is then skipped —
    the endpoint tests patch the stage `run` anyway)."""
    monkeypatch.setattr("src.api.app.load_catalog", AsyncMock(return_value=None))
