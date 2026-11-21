"""The IPTC concept catalog, pulled from the Karakutu Server API (`GET /concepts`).

Why from the API and not a vendored file: the archive is the source of truth for the
vocabulary its auditors tag with (`subject_tag_notations` = `medtop:*`, `content_type_notation`
= `genre:*`), and it tracks IPTC releases. Pulling it keeps
this service's tags valid against the archive's validator (`^(medtop|genre):[A-Za-z0-9_-]+$`).

Locale is pinned to en-GB: it is the only locale with a label AND a definition for every one of
the 1,393 media topics (tr covers 641 labels and no definitions). The definitions are what the
classifier reads.

The engine loads the catalog once at startup and captures it with each classification step.
An unavailable catalog is explicit: classification is skipped, never invented.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict


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
