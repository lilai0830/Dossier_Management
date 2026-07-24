"""
Retriever — discover relevant pages for a project at package time.

Summary pages are found with LexicalRetriever using TWO INDEPENDENT, PARALLEL
selectors (page-selection stage, distinct from document classification which
uses classify/*.txt). Neither selector depends on the other's output:

  A) Keyword selector   — rank every page of a type by lexical score against
     queries/{CLINICAL,FE,CE}.txt and keep the top `top_n` (the "summary pages",
     on-topic, high textual relevance).
  B) Structure selector — rank every page of a type by structural richness
     (figures + table + list) and keep the top `top_n` (the "figure /
     information-dense pages").

After merging, each type is capped at `top_n` pages (default TOP_N_PER_TYPE = 12,

The two shortlists are produced side by side and then MERGED (union,
deduplicated) at the very end. A page may be picked by A, by B, or by both;
the merge records which selector(s) chose it.

Zero extra dependencies, deterministic, explainable.

The pipeline deliberately uses a lightweight lexical retriever instead of a
vector database: the corpus is a small, structured set of project reports,
and the analysis frame (queries/*.txt) is already expressed as the retrieval
condition, so keyword matching is both sufficient and fully transparent.

All implementations return a list of items shaped like:
    {"metadata": {report_type, filename, page_index, page_label, source_path,
                  matched_terms, selected_by},
     "document": "<page text>"}
"""

import re
from abc import ABC, abstractmethod
from collections import Counter

from .config import (
    REPORT_TYPES,
    TOP_N_PER_TYPE,
)
from .logger import get_logger
from .page_index import load_index, index_count, delete_index

logger = get_logger("retriever")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_retriever(
    mode: str | None = None,
    project_id: str = "default",
) -> "Retriever":
    """Construct the retriever for a project.

    Only the lexical retriever is supported (no vector DB / embedding model).
    The ``mode`` argument is accepted for backward compatibility but ignored.
    """
    return LexicalRetriever(project_id)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class Retriever(ABC):
    """Discovers relevant pages for one project given per-type query text."""

    def __init__(self, project_id: str):
        self.project_id = project_id

    @abstractmethod
    def discover(self, queries: dict[str, str]) -> list[dict]:
        """Return page items ({"metadata", "document"}) for merging."""
        ...

    @abstractmethod
    def count(self) -> int:
        """Number of indexable pages available for this project."""
        ...

    def reset(self) -> None:
        """Drop any persisted state for this project."""
        delete_index(self.project_id)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _to_items(pages: list[dict]) -> list[dict]:
    """Convert stored page records into pipeline item shape."""
    return [{
        "metadata": {
            "report_type": p["report_type"],
            "filename": p["filename"],
            "page_index": p["page_index"],
            "page_label": p["page_label"],
            "source_path": p["source_path"],
            "matched_terms": p.get("matched_terms", []),
            "selected_by": p.get("selected_by", []),
        },
        "document": p["text"],
    } for p in pages]


def _token_weights(query: str) -> Counter:
    """Term -> weight (occurrences in the query). Longer terms weigh more
    because the user repeated/emphasized them."""
    toks = re.findall(r"[a-z0-9]+", query.lower())
    return Counter(t for t in toks if len(t) > 2)


def _lexical_score(text: str, query: str, weights: Counter):
    """Weighted count of matched query terms + a phrase-match bonus.

    Returns (score, matched_terms) where matched_terms is the list of query
    terms found in the page text (used for the PDF annotation block).
    """
    lowered = text.lower()
    score = 0.0
    matched: list[str] = []
    for term, w in weights.items():
        if term in lowered:
            score += w
            matched.append(term)
    # Bonus for whole query lines appearing verbatim (e.g. a key phrase).
    for phrase in query.lower().split("\n"):
        phrase = phrase.strip()
        if len(phrase) > 4 and phrase in lowered:
            score += 2.0
    return score, matched


def _structural_score(page: dict) -> float:
    """Structural richness of a page, used by the (parallel) structure selector.

    A page is "information-dense" when it carries figures, a table, or a list.
    Weighting: each figure counts 1, a table counts 2 (highly structured data),
    a bullet/numbered list counts 1. Pages with score 0 are pure prose and are
    NOT candidates for the structure selector.

    Old index records may lack the ``signals`` field -> score 0.
    """
    sig = page.get("signals") or {}
    score = float(sig.get("figures", 0))
    if sig.get("table"):
        score += 2.0
    if sig.get("bullets"):
        score += 1.0
    return score


# ---------------------------------------------------------------------------
# Lexical (default & only retriever)
# ---------------------------------------------------------------------------

class LexicalRetriever(Retriever):
    """Keyword/term-list scoring — no embeddings, no vector store."""

    def count(self) -> int:
        return index_count(self.project_id)

    def discover(
        self,
        queries: dict[str, str],
        top_n: int | None = None,
    ) -> list[dict]:
        """Select pages for the synthesis PDF with TWO PARALLEL selectors.

        For each report type, two independent shortlists are built side by side
        (neither filters the other's input):

          A) Keyword selector   — rank ALL pages of the type by lexical score
             against queries/{type}.txt; keep the top ``top_n`` pages that
             actually match (score > 0). These are summary pages.
          B) Structure selector — rank ALL pages of the type by structural
             richness (figures + table + list); keep the top ``top_n`` pages
             that carry any structure (score > 0). These are figure /
             information-dense pages.

        The two shortlists are then MERGED (union, deduplicated by
        source_path + page_index). After merging, EACH report type keeps at
        most ``top_n`` pages (the final cap). ``top_n`` is configurable at
        runtime (frontend input / CLI --top-n); when omitted it falls back to
        ``config.TOP_N_PER_TYPE``. A ``top_n`` of ``-1`` is a sentinel meaning
        "no per-type cap" (All) — every page chosen by either selector is kept.

        Each surviving page is tagged with which selector(s) chose it
        (``selected_by``: ["keyword"], ["structure"], or both). Ordering
        within a type: pages chosen by both selectors first, then by combined
        relevance.
        """
        pages = load_index(self.project_id)
        if not pages:
            return []

        if top_n is None:
            cap = TOP_N_PER_TYPE
        elif top_n == -1:
            cap = None            # sentinel: no per-type cap (All)
        elif isinstance(top_n, int) and top_n > 0:
            cap = top_n
        else:
            cap = TOP_N_PER_TYPE

        results: list[dict] = []
        for rt in REPORT_TYPES:
            q = queries.get(rt, "").strip()
            candidates = [p for p in pages if p["report_type"] == rt]
            if not candidates:
                continue

            # Compute BOTH scores for every page up front (parallel, no gating).
            weights = _token_weights(q) if q else Counter()
            for p in candidates:
                s, matched = (
                    _lexical_score(p.get("text", ""), q, weights)
                    if q else (0.0, [])
                )
                p["_kw_score"] = s
                p["matched_terms"] = matched
                p["_struct_score"] = _structural_score(p)

            # Selector A — keyword shortlist (summary pages).
            kw_hits = [p for p in candidates if p["_kw_score"] > 0]
            kw_hits.sort(key=lambda p: (-p["_kw_score"], -p["_struct_score"]))
            kw_selected = kw_hits[:cap]

            # Selector B — structure shortlist (figure / info-dense pages).
            st_hits = [p for p in candidates if p["_struct_score"] > 0]
            st_hits.sort(key=lambda p: (-p["_struct_score"], -p["_kw_score"]))
            st_selected = st_hits[:cap]

            # Merge (union) at the end, deduplicated, with selector tags.
            merged: dict[tuple, dict] = {}
            for p in kw_selected:
                key = (p["source_path"], p["page_index"])
                p["selected_by"] = ["keyword"]
                merged[key] = p
            for p in st_selected:
                key = (p["source_path"], p["page_index"])
                if key in merged:
                    merged[key]["selected_by"].append("structure")
                else:
                    p["selected_by"] = ["structure"]
                    merged[key] = p

            # Order: chosen-by-both first, then combined relevance.
            ordered = sorted(
                merged.values(),
                key=lambda p: (
                    -len(p["selected_by"]),
                    -p["_kw_score"],
                    -p["_struct_score"],
                ),
            )

            # Final per-type cap: this type keeps at most `cap` pages.
            ordered = ordered[:cap]

            both = sum(1 for p in ordered if len(p["selected_by"]) == 2)
            cap_label = "All (no cap)" if cap is None else cap
            logger.info(
                f"[{rt}] kept {len(ordered)} pages "
                f"(keyword={len(kw_selected)}, structure={len(st_selected)}, "
                f"both={both}) from {len(candidates)} candidates "
                f"(cap per type = {cap_label})"
            )
            results.extend(_to_items(ordered))

        return results
