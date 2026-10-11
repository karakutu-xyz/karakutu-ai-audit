"""The IPTC concept catalog, pulled from the Karakutu Server API (`GET /concepts`).

Why from the API and not a vendored file: the archive is the source of truth for the
vocabulary its auditors tag with (`subject_tag_notations` = `medtop:*`, `content_type_notation`
= `genre:*`), and it tracks IPTC releases (2026-07-02 at the time of writing). Pulling it keeps
this service's tags valid against the archive's validator (`^(medtop|genre):[A-Za-z0-9_-]+$`).

Locale is pinned to en-GB: it is the only locale with a label AND a definition for every one of
the 1,393 media topics (tr covers 641 labels and no definitions). The definitions are what the
classifier reads.

The catalog is loaded ONCE (app lifespan / batch runner) and cached to disk so a restart while
the archive is down still classifies. No API and no cache => `None`, and step 1 is skipped.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

SCHEME_TOPICS = "iptc_mediatopic"
SCHEME_GENRES = "iptc_genre"
CATALOG_LOCALE = "en-GB"


class Concept(BaseModel):
    """One vocabulary entry (spec: `ConceptResponse`). Only the fields we use."""
    model_config = ConfigDict(extra="ignore")

    notation: str
    scheme: str
    pref_label: str
    definition: Optional[str] = None
    parent_notation: Optional[str] = None
    hierarchy_path: Optional[str] = None   # "02000000.20000082.20000093"; None for genres

    @property
    def depth(self) -> int:
        """1 for a top-level topic (and for genres), +1 per dot in the hierarchy path."""
        return self.hierarchy_path.count(".") + 1 if self.hierarchy_path else 1


class ConceptCatalog:
    """In-memory, read-only view of the vocabulary with the hierarchy helpers step 1 needs."""

    def __init__(self, topics: Iterable[Concept], genres: Iterable[Concept]):
        self._topics: Dict[str, Concept] = {c.notation: c for c in topics}
        self._genres: Dict[str, Concept] = {c.notation: c for c in genres}
        self._children: Dict[Optional[str], List[str]] = defaultdict(list)
        for c in self._topics.values():
            self._children[c.parent_notation].append(c.notation)
        for kids in self._children.values():
            kids.sort()

    @property
    def topic_count(self) -> int:
        return len(self._topics)

    @property
    def genre_count(self) -> int:
        return len(self._genres)

    def get(self, notation: str) -> Optional[Concept]:
        return self._topics.get(notation) or self._genres.get(notation)

    def is_topic(self, notation: str) -> bool:
        return notation in self._topics

    def is_genre(self, notation: str) -> bool:
        return notation in self._genres

    def label(self, notation: str) -> str:
        c = self.get(notation)
        return c.pref_label if c else notation

    def top_level(self) -> List[Concept]:
        return [self._topics[n] for n in self._children.get(None, [])]

    def children(self, notation: str) -> List[Concept]:
        return [self._topics[n] for n in self._children.get(notation, [])]

    def subtree(self, notation: str, max_depth: int) -> List[Concept]:
        """Descendants of `notation` whose ABSOLUTE depth is <= max_depth, in depth-first
        hierarchy order (a parent immediately precedes its children)."""
        out: List[Concept] = []

        def walk(parent: str) -> None:
            for child in self.children(parent):
                if child.depth > max_depth:
                    continue
                out.append(child)
                walk(child.notation)

        walk(notation)
        return out

    def genres(self) -> List[Concept]:
        return [self._genres[n] for n in sorted(self._genres)]

    def to_json(self) -> dict:
        return {
            "locale": CATALOG_LOCALE,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "topics": [c.model_dump() for c in self._topics.values()],
            "genres": [c.model_dump() for c in self._genres.values()],
        }

    @classmethod
    def from_json(cls, data: dict) -> "ConceptCatalog":
        return cls(
            (Concept.model_validate(c) for c in data.get("topics", [])),
            (Concept.model_validate(c) for c in data.get("genres", [])),
        )


async def load_catalog(client, cache_path: Path) -> Optional[ConceptCatalog]:
    """Pull both schemes from the archive; on success refresh the cache, on failure read it.

    `client` is a `KarakutuClient` (or any object with `.enabled` and `.list_concepts`).
    Returns None when neither the API nor the cache can supply BOTH schemes — half a
    vocabulary would silently mis-tag, so it is all or nothing.
    """
    cache_path = Path(cache_path)
    topics: List[Concept] = []
    genres: List[Concept] = []
    if getattr(client, "enabled", False):
        topics = await client.list_concepts(SCHEME_TOPICS)
        if topics:
            genres = await client.list_concepts(SCHEME_GENRES)
    else:
        logger.info("concepts: archive client disabled — not pulling the vocabulary")

    if topics and genres:
        catalog = ConceptCatalog(topics, genres)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(catalog.to_json(), ensure_ascii=False))
        except OSError as exc:
            logger.warning("concepts: could not write cache %s (%s)", cache_path,
                           type(exc).__name__)
        logger.info("concepts: loaded %d media topics and %d genres from the archive",
                    catalog.topic_count, catalog.genre_count)
        return catalog

    try:
        data = json.loads(cache_path.read_text())
        catalog = ConceptCatalog.from_json(data)
    except (OSError, ValueError) as exc:
        logger.warning("concepts: no vocabulary — archive gave nothing and cache %s is "
                       "unusable (%s); classification will be skipped", cache_path,
                       type(exc).__name__)
        return None
    if catalog.topic_count == 0 or catalog.genre_count == 0:
        return None
    logger.warning("concepts: archive gave nothing — using cached vocabulary from %s "
                   "(fetched %s)", cache_path, data.get("fetched_at", "?"))
    return catalog
