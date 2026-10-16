"""Response models for the Karakutu Server API.

Mirrors the shapes in the OpenAPI spec (Karakutu Server 0.2.81-beta). Only the fields this
service actually uses are declared; `model_config = ConfigDict(extra="ignore")` lets the
server add fields without breaking us — an audit must never fail because the archive grew a
column.
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class NewspaperRef(BaseModel):
    """The publisher an article belongs to."""
    model_config = ConfigDict(extra="ignore")

    slug: str = ""
    display_name: str = ""
    country: Optional[str] = None


class Author(BaseModel):
    """A byline. The API returns objects (`{"id": 12, "name": "Haber Merkezi"}`), not strings.

    Note the archive's own caveat: `authors` is sometimes empty even when the
    source page had a byline, because the per-outlet author mapper doesn't cover every outlet.
    """
    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None
    name: str = ""


class VersionEntry(BaseModel):
    """One archived snapshot of an article (`expand=versions`).

    Matters because a censored article's TOP-LEVEL `news_text` is usually empty — the last
    fetch was a 404. The body only survives in an earlier snapshot, so the real text has to
    be recovered by walking these and taking the longest (guide §8).
    """
    model_config = ConfigDict(extra="ignore")

    details_id: Optional[int] = None
    title: str = ""
    description: str = ""
    news_text: str = ""
    check_date: Optional[int] = None
    content_hash: Optional[str] = None


class ArticleRecord(BaseModel):
    """An article as the archive stores it (spec: `ArticleResponse`).

    `id` is the handle everything else keys off — `/articles/{id}/similar` needs it, and the
    only way this service can obtain one is `GET /articles?url=…`, since an audit request
    carries a URL, not an id.
    """
    model_config = ConfigDict(extra="ignore")

    id: int
    url: str
    title: str = ""
    description: str = ""
    news_text: str = ""
    status: Optional[int] = None
    publish_date: Optional[str] = None
    language_code: Optional[str] = None
    newspaper: Optional[NewspaperRef] = None
    authors: List[Author] = Field(default_factory=list)
    versions: List[VersionEntry] = Field(default_factory=list)


class SimilarArticle(BaseModel):
    """One similarity hit (spec: `SimilarArticleItem`).

    The server scores similarity itself (url/title/date proximity), so these are ranked leads,
    not raw search results. `link` is the article URL — note the field is `link`, NOT `url`.
    """
    model_config = ConfigDict(extra="ignore")

    id: int
    link: str
    title: Optional[str] = None
    description: Optional[str] = None
    publish_date: Optional[str] = None
    newspaper: Optional[str] = None
    status: Optional[int] = None
    similarity_score: float = 0.0
    url_similarity: float = 0.0
    title_similarity: float = 0.0
    date_proximity_days: int = 0
    date_proximity_score: float = 0.0


class SimilarArticlesResult(BaseModel):
    """Spec: `SimilarArticlesResponse`."""
    model_config = ConfigDict(extra="ignore")

    total: int = 0
    articles: List[SimilarArticle] = Field(default_factory=list)


def longest_body(record: "ArticleRecord") -> str:
    """The most substantial archived body text for an article.

    A censored article's live `news_text` is typically empty (the URL 404s now), so the real
    text lives in an earlier snapshot. Take the longest non-empty one; fall back to whatever
    the top level has.
    """
    texts = [v.news_text for v in record.versions if v.news_text]
    if record.news_text:
        texts.append(record.news_text)
    return max(texts, key=len) if texts else ""
