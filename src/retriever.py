"""
Retriever — discover relevant pages for a project at package time.

Summary pages are found with LexicalRetriever using TWO INDEPENDENT, PARALLEL
selectors (page-selection stage, distinct from document classification which
uses classify/*.txt). Neither selector depends on the other's output:

  A) Keyword selector   — rank every page of a type by lexical score against
     queries/{CLINS,FE,CE}.txt and keep the top `top_n` (the "summary pages",
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

import math
import re
from abc import ABC, abstractmethod
from collections import Counter

from .config import (
    REPORT_TYPES,
    TOP_N_PER_TYPE,
    LEXICON_DIMENSIONS,
    DIM_WEIGHTS,
    TF_SUBLINEAR,
    TITLE_ANCHOR_TOP_REGION,
    TITLE_ANCHOR_BOOST,
    TABLE_FEATURE_SYNERGY,
    TOC_HEADERS,
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


def _is_toc_page(page: dict) -> bool:
    """Detect a Table-of-Contents / cover page so the structure selector does
    not mistake its long bullet/numbered list for an 'information-dense' page.

    A TOC page is recognised when one of TOC_HEADERS appears in the first few
    normalised lines of the page text.
    """
    text = page.get("text", "") or page.get("_norm_text", "")
    if not text:
        return False
    head = [normalize(line) for line in text.splitlines()[:5]]
    for line in head:
        for hdr in TOC_HEADERS:
            if hdr in line:
                return True
    return False


def _structural_score(page: dict) -> float:
    """Structural richness of a page, used by the (parallel) structure selector.

    A page is "information-dense" when it carries figures, a table, or a list.
    Weighting: each figure counts 1, a table counts 2 (highly structured data),
    a bullet/numbered list counts 1. Pages with score 0 are pure prose and are
    NOT candidates for the structure selector.

    A detected Table-of-Contents / cover page is zeroed out (its bullet list is
    not a summary signal) — see _is_toc_page.

    Old index records may lack the ``signals`` field -> score 0.
    """
    if _is_toc_page(page):
        return 0.0
    sig = page.get("signals") or {}
    score = float(sig.get("figures", 0))
    if sig.get("table"):
        score += 2.0
    if sig.get("bullets"):
        score += 1.0
    return score


# ---------------------------------------------------------------------------
# TF-IDF (weighted, 4-dimension lexicon) keyword scorer  [page-selection]
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase, strip punctuation (keep alphanumerics + spaces), collapse
    whitespace. Makes token and phrase matching punctuation-agnostic so e.g.
    'Skin-elasticity' and 'Skin elasticity' normalise identically, and avoids
    the old naive substring bug ('log' matching 'biology').
    """
    s = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", s).strip()


SECTION_ALIASES = {
    "title anchors": "title_anchors",
    "title anchor": "title_anchors",
    "metric keywords": "metric_keywords",
    "metric keyword": "metric_keywords",
    "table features": "table_features",
    "table feature": "table_features",
    "other": "other",
}


def parse_lexicon(raw: str) -> dict[str, list[str]]:
    """Parse a sectioned queries/{type}.txt into {dimension: [terms]}.

    Sections are introduced by a '# <Dimension>' header line. Any other '#'
    line is a comment. One term/phrase per line; multi-word entries are kept
    intact as phrases. A file with no section headers (legacy flat format)
    falls back to the 'metric_keywords' dimension so callers stay compatible.
    """
    dims: dict[str, list[str]] = {d: [] for d in LEXICON_DIMENSIONS}
    current: str | None = None
    found_section = False
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            header = s[1:].strip().lower()
            mapped = None
            for alias, dim in SECTION_ALIASES.items():
                if alias == header or header.startswith(alias) or header.endswith(alias):
                    mapped = dim
                    break
            if mapped:
                current = mapped
                found_section = True
            else:
                current = None  # comment line
            continue
        if current is None:
            if not found_section:
                current = "metric_keywords"  # legacy flat fallback
            else:
                continue  # comment-only region between sections
        dims[current].append(s)
    return dims


def compute_idf(pages: list[dict], lexicon: dict[str, list[str]]) -> dict[str, float]:
    """BM25-style IDF for every lexicon term, computed PER report type over the
    pages of that type.

    idf(t) = log(1 + (N - df + 0.5) / (df + 0.5)), N = #pages of the type.
    Rare, distinctive terms (small df) get a high weight; boilerplate terms that
    appear on almost every page (large df) are pushed toward 0.
    """
    n = len(pages)
    if n == 0:
        return {}
    idf: dict[str, float] = {}
    for dim, terms in lexicon.items():
        for term in terms:
            key = normalize(term)
            if not key or key in idf:
                continue
            df = 0
            for p in pages:
                toks = p.get("_tokens")
                if toks is None:
                    toks = normalize(p.get("text", "")).split()
                if " " in key:
                    hay = p.get("_norm_text")
                    if hay is None:
                        hay = normalize(p.get("text", ""))
                    if key in hay:
                        df += 1
                elif key in toks:
                    df += 1
            idf[key] = (
                math.log(1 + (n - df + 0.5) / (df + 0.5)) if df > 0
                else math.log(1 + (n + 0.5) / 0.5)
            )
    return idf


def _tfidf_score(
    page: dict,
    lexicon: dict[str, list[str]],
    idf: dict[str, float],
) -> tuple[float, list[str]]:
    """Weighted TF-IDF score of one page against the 4-dimension lexicon.

    score = Σ_t TF(t,page) · IDF(t) · W(dim(t)) · Pos(t)
      TF   : 1 + log(count) when TF_SUBLINEAR else raw count
      IDF  : from compute_idf (per-type)
      W    : DIM_WEIGHTS[dim]
      Pos  : Title Anchors in the heading zone (top TITLE_ANCHOR_TOP_REGION of
             the page) get ×TITLE_ANCHOR_BOOST; Table Features co-occurring with
             a detected table get ×TABLE_FEATURE_SYNERGY.
    Multi-dimension hits (co-occurrence on one page) are rewarded by summation.
    Returns (score, matched_terms) where matched_terms are the raw lexicon
    entries for the PDF annotation block.
    """
    score = 0.0
    matched: list[tuple[str, float]] = []   # (term, per-term weight)
    norm_text = page.get("_norm_text") or normalize(page.get("text", ""))
    tokens = page.get("_tokens")
    if tokens is None:
        tokens = norm_text.split()
    top_text = page.get("_top_text")
    if top_text is None:
        top_k = max(1, int(len(tokens) * TITLE_ANCHOR_TOP_REGION))
        top_text = " ".join(tokens[:top_k])
    has_table = bool((page.get("signals") or {}).get("table"))
    tf_func = (lambda c: 1.0 + math.log(c)) if TF_SUBLINEAR else (lambda c: float(c))

    for dim, terms in lexicon.items():
        w_dim = DIM_WEIGHTS.get(dim, 1.0)
        for term in terms:
            key = normalize(term)
            if not key:
                continue
            if " " in key:                      # phrase
                cnt = norm_text.count(key)
            else:                               # whole-word token
                cnt = tokens.count(key)
            if cnt == 0:
                continue
            contrib = tf_func(cnt) * idf.get(key, 0.0) * w_dim
            if dim == "title_anchors" and key in top_text:
                contrib *= TITLE_ANCHOR_BOOST
            if dim == "table_features" and has_table:
                contrib *= TABLE_FEATURE_SYNERGY
            score += contrib
            matched.append((term, contrib))
    # Sort matched terms by their per-term weight (TF·IDF·dim·boost) descending
    # so the footer surfaces the most salient terms first, then strip back to a
    # plain list[str] so every downstream consumer is unchanged.
    matched.sort(key=lambda x: -x[1])
    return score, [t for t, _ in matched]


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

            # Pre-normalise page text once (used by both IDF and scoring).
            for p in candidates:
                nt = normalize(p.get("text", ""))
                toks = nt.split()
                p["_norm_text"] = nt
                p["_tokens"] = toks
                n = len(toks)
                top_k = max(1, int(n * TITLE_ANCHOR_TOP_REGION))
                p["_top_tokens"] = set(toks[:top_k])
                p["_top_text"] = " ".join(toks[:top_k])

            # Compute BOTH scores for every page up front (parallel, no gating).
            lexicon = parse_lexicon(q) if q else {}
            idf = compute_idf(candidates, lexicon) if lexicon else {}
            for p in candidates:
                # A Table-of-Contents / cover page is navigation, not content:
                # exclude it from BOTH selectors. Zeroing both scores drops it
                # from the keyword shortlist (it often lists every section
                # heading = high-weight Title Anchors) and from the structure
                # shortlist (its long bullet list would look "info-dense").
                if _is_toc_page(p):
                    p["_kw_score"] = 0.0
                    p["matched_terms"] = []
                    p["_struct_score"] = 0.0
                    continue
                if lexicon:
                    s, matched = _tfidf_score(p, lexicon, idf)
                else:
                    s, matched = 0.0, []
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
