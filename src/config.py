"""
Dossier_Management — Pipeline Configuration
"""

import os
from pathlib import Path

# --- Project root ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Data & output directories ---
DATA_DIR = PROJECT_ROOT / "data"
INDEX_DIR = PROJECT_ROOT / "index_projects"   # lightweight page-text index (no vectors)
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
QUERIES_DIR = PROJECT_ROOT / "queries"
CLASSIFY_DIR = DATA_DIR / "inbox"
CLASSIFY_PROFILE_DIR = PROJECT_ROOT / "classify"

# --- Report type subdirectories ---
REPORT_TYPES = ["CLINICAL", "FE", "CE"]


def project_data_dir(project_name: str) -> Path:
    """Per-project dossier working folder: PROJECT_ROOT / <project_name>/.

    This is the folder the app scans for a project's raw dossiers (the user
    keeps their source files here, typically inside an OneDrive-synced tree).
    Classified files are written into <project_name>/{CLINICAL,FE,CE}/ beneath
    it. The project name doubles as the pipeline ``project_id`` (index key +
    output PDF name), so this single mapping drives the whole per-project flow.
    """
    return PROJECT_ROOT / project_name

# Friendly labels for the synthesis PDF annotation block (user-defined mapping:
# CLINICAL = clinical signal, FE = sensory signal, CE = consumer evaluation signal).
REPORT_TYPE_LABELS = {
    "CLINICAL": "Clinical",
    "FE": "Sensory",
    "CE": "Consumer Evaluation",
}

# --- PDF parsing ---
SCREENSHOT_DPI = 300          # High resolution for crisp screenshots (~4x default 72 DPI)

# --- Page-selection (retriever) tuning --------------------------------
# Two INDEPENDENT, PARALLEL selectors run per report type and are merged at the
# end (union). Neither depends on the other:
#   A) Keyword selector  — ranks pages by lexical score against queries/{type}.txt
#      (the "summary pages").
#   B) Structure selector — ranks pages by structural richness (figures + table +
#      list) (the "figure / info-dense pages").
# After merging, EACH report type keeps at most TOP_N_PER_TYPE pages, so the
# final PDF stays focused. The value is NOT hardcoded at the call site — it can
# be overridden at runtime via the frontend input or the CLI flag --top-n, and
# falls back to this default only when no override is given.
TOP_N_PER_TYPE = 12            # default cap of pages kept PER report type

# Backward-compatible alias (older imports referenced LEXICAL_TOP_N_PER_TYPE).
LEXICAL_TOP_N_PER_TYPE = TOP_N_PER_TYPE

# --- Default per-type queries (fallback if queries/*.txt missing) ---
DEFAULT_QUERIES: dict[str, str] = {
    "CLINICAL": (
        "clinical study trial investigation efficacy safety dermatological "
        "tolerance adverse event endpoint instrumental measurement before after "
        "grader assessment clinical outcome claim substantiation"
    ),
    "FE": (
        "sensory evaluation panel analysis texture fragrance odor appearance "
        "feel sensory attributes descriptive analysis hedonic liking touch "
        "color sensory profiling results technical summary"
    ),
    "CE": (
        "consumer evaluation clinical study efficacy safety dermatological "
        "tolerance satisfaction self-assessment instrumental measurement "
        "statistical analysis primary endpoint results conclusion"
    ),
}

# --- Document-type classification (auto-sort uploaded PDFs) -----------
# A document's first-page text is scored (keyword/term-list) against each
# type's profile in classify/{TYPE}.txt. Highest score wins. Low confidence
# forces manual review. No embedding model is used.
CLASSIFY_MIN_SCORE = 1          # min lexical score for a type to be a candidate
CLASSIFY_CONFIDENCE_MARGIN = 1  # top1 - top2 gap below this => low confidence

DEFAULT_CLASSIFY_PROFILES: dict[str, str] = {
    "CLINICAL": (
        "Clinical report. First page identifies a clinical study, clinical trial, "
        "clinical evaluation, or clinical investigation. Contains terms: clinical, "
        "investigator, dermatological, efficacy, tolerance, safety, before/after, "
        "grader assessment, instrumental clinical measurement, endpoints, "
        "adverse event. Measured clinical outcomes under controlled conditions."
    ),
    "FE": (
        "Sensory report. First page identifies sensory evaluation, sensory analysis, "
        "sensory panel, or sensory assessment. Contains terms: sensory, panel, "
        "texture, fragrance, odor, appearance, feel, sensory attributes, "
        "descriptive analysis, hedonic, liking, touch, color. Product assessed "
        "through human sensory perception."
    ),
    "CE": (
        "Consumer Evaluation report. First page identifies a consumer test, "
        "consumer evaluation, consumer study, or consumer panel. Contains terms: "
        "consumer, evaluation, self-assessment, usage test, home-use test, "
        "questionnaire, satisfaction, perception, consumer feedback, claim "
        "substantiation, panelist. Measures consumer opinion and behavior at scale."
    ),
}

# --- PDF output ---
# A4 is used directly in pdf_generator.py via reportlab.lib.pagesizes

# --- Target-formula banner (Metadata Injection) -----------------------
# Baked into the cover of the synthesis-input PDF so the downstream
# multimodal LLM is anchored on the final target formula and treats
# data about other formulas as development reference only. {formula}
# is replaced by the user-supplied target formula string.
TARGET_FORMULA_BANNER_TEMPLATE = (
    "The final target formula for this Synthesis is: {formula}. "
    "Any data in the reports concerning other formulas is provided "
    "for development-reference purposes only."
)

# --- Logging ---
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# --- Ensure directories exist ---
for d in [
    DATA_DIR,
    INDEX_DIR,
    SCREENSHOTS_DIR,
    OUTPUT_DIR,
    LOG_DIR,
    QUERIES_DIR,
    CLASSIFY_DIR,
    CLASSIFY_PROFILE_DIR,
]:
    d.mkdir(parents=True, exist_ok=True)

for rt in REPORT_TYPES:
    (DATA_DIR / rt).mkdir(parents=True, exist_ok=True)
