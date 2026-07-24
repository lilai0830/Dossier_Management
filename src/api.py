"""
FastAPI server — Dossier_Management Document Pipeline

Endpoints:
  POST /project/scan     — Scan PROJECT_ROOT/<name>/ for dossier files
  POST /classify         — Auto-classify dossiers in the project folder
  POST /classify/confirm — Apply final type decisions (move files)
  GET  /classify/profiles     — Read type profiles from classify/*.txt
  POST /classify/profiles/save — Save type profiles to classify/*.txt
  POST /ingest           — Trigger ingest (reads <project>/{CLINICAL,FE,CE}/)
  POST /package          — Trigger package → generate synthesis PDF
  POST /run              — One-click: ingest + package
  GET  /status           — Index stats
  GET  /download/{pid}  — Download the output PDF
  GET  /queries          — Read per-type query texts from queries/*.txt
  POST /queries/save     — Save per-type query texts to queries/*.txt
  POST /reset            — Reset project (index + screenshots only)
  POST /clear-reset      — Safe reset: index + screenshots + output PDF only
  GET  /                — Serves the frontend UI
"""

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import (
    OUTPUT_DIR,
    PROJECT_ROOT,
    REPORT_TYPES,
    SCREENSHOTS_DIR,
    project_data_dir,
)
# NOTE: DATA_DIR (the legacy global data/ folder) is intentionally no longer
# imported here — the pipeline now reads/writes per-project folders
# (PROJECT_ROOT/<project_id>/{CLINICAL,FE,CE}/) instead of a shared data/ tree.
from .logger import get_logger
from .pipeline import (
    DossierPipeline,
    load_queries_from_files,
    save_queries_to_files,
)
from .classifier import (
    Classifier,
    load_profiles_from_files as load_classify_profiles,
    save_profiles_to_files as save_classify_profiles,
)
from .converter import _is_junk_filename
from .page_index import delete_index, index_exists

logger = get_logger("api")


def _is_dossier_ext(name: str) -> bool:
    """True for the document extensions the pipeline accepts."""
    return Path(name).suffix.lower() in (".pdf", ".pptx", ".docx")


app = FastAPI(title="Dossier_Management Document Pipeline", version="2.0")

# Serve static files (CSS, JS, etc.)
app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT_ROOT / "static")),
    name="static",
)
# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class PackageRequest(BaseModel):
    project_id: str = "default"
    queries: Optional[dict[str, str]] = None
    top_n: Optional[int] = None  # cap of pages PER report type; -1 = All (no cap); None = config default
    project_owner: str = ""  # name of the project owner, shown on the PDF cover
    target_formula: str = ""  # final target formula; baked into the PDF cover


class RunRequest(BaseModel):
    project_id: str = "default"
    queries: Optional[dict[str, str]] = None
    top_n: Optional[int] = None  # cap of pages PER report type; -1 = All (no cap); None = config default
    project_owner: str = ""  # name of the project owner, shown on the PDF cover
    target_formula: str = ""  # final target formula; baked into the PDF cover


class ScanRequest(BaseModel):
    project_name: str  # folder name under PROJECT_ROOT to scan for dossiers


class QueriesSaveRequest(BaseModel):
    queries: dict[str, str]

class ClassifyConfirmRequest(BaseModel):
    decisions: list[dict] = []   # [{"filename": ..., "report_type": ...}]
    # project_id is taken from the query string (consistent with /classify),
    # NOT the body — the frontend sends it as ?project_id=... alongside the
    # decisions payload. Declaring it here too would shadow/silently default
    # it to "default" and break per-project sorting.

class ClassifyProfileSaveRequest(BaseModel):
    profiles: dict[str, str]

# ---------------------------------------------------------------------------
# Global pipeline state (one project at a time)
# ---------------------------------------------------------------------------

_pipeline: DossierPipeline | None = None


def _get_pipeline(project_id: str = "default") -> DossierPipeline:
    global _pipeline
    if _pipeline is None or _pipeline.project_id != project_id:
        _pipeline = DossierPipeline(project_id)
        _pipeline.init()
    return _pipeline


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the frontend UI."""
    index_path = PROJECT_ROOT / "static" / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>Frontend not found. Place index.html in static/</h2>")


@app.post("/project/scan")
async def scan_project(req: ScanRequest):
    """Scan a project folder for dossier files.

    The project folder is PROJECT_ROOT/<project_name>/. Top-level dossier
    files (pdf/pptx/docx, excluding Office lock files / OS cruft) are listed.
    Already-classified subfolders (CLINICAL/FE/CE) are ignored so a re-scan
    does not double-count.
    """
    name = (req.project_name or "").strip()
    if not name:
        raise HTTPException(400, "project_name is required")

    folder = project_data_dir(name)
    if not folder.exists():
        raise HTTPException(
            404,
            f"Project folder not found: {folder}",
        )

    files = []
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        if _is_junk_filename(p.name):
            continue
        if not _is_dossier_ext(p.name):
            continue
        files.append({
            "filename": p.name,
            "size_kb": round(p.stat().st_size / 1024, 1),
            "type": p.suffix.lower().lstrip(".").upper(),
        })

    return {
        "ok": True,
        "project_name": name,
        "folder": str(folder),
        "files": files,
        "count": len(files),
    }


@app.post("/ingest")
async def ingest(project_id: str = "default"):
    """Run ingest: parse all PDFs in PROJECT_ROOT/<project_id>/{CLINICAL,FE,CE}/ and build the index."""
    pipeline = _get_pipeline(project_id)
    try:
        count = pipeline.ingest()
        return {
            "ok": True,
            "project_id": project_id,
            "pages_ingested": count,
            "total_pages": pipeline.total_pages,
        }
    except Exception as e:
        logger.exception("Ingest failed")
        raise HTTPException(500, str(e))


@app.post("/package")
async def package(req: PackageRequest):
    """Run package: per-type lexical match → group → screenshot → merge PDF.

    Body (JSON):
        project_id: str  (default "default")
        queries: dict[str, str] | null  (optional per-type query overrides)
    """
    pipeline = _get_pipeline(req.project_id)
    try:
        output_path = pipeline.package(
            req.queries, top_n=req.top_n,
            project_owner=req.project_owner, target_formula=req.target_formula,
        )
        return {
            "ok": True,
            "project_id": req.project_id,
            "output_file": output_path.name,
            "output_path": str(output_path),
        }
    except Exception as e:
        logger.exception("Package failed")
        raise HTTPException(500, str(e))


@app.post("/run")
async def run_pipeline(req: RunRequest):
    """One-click: ingest + package.

    Body (JSON):
        project_id: str  (default "default")
        queries: dict[str, str] | null  (optional per-type query overrides)
    """
    pipeline = _get_pipeline(req.project_id)
    try:
        n = pipeline.ingest()
        logger.info(f"Ingested {n} pages for project '{req.project_id}'")
        output_path = pipeline.package(
            req.queries, top_n=req.top_n,
            project_owner=req.project_owner, target_formula=req.target_formula,
        )
        logger.info(f"Package complete -> {output_path}")
        return {
            "ok": True,
            "project_id": req.project_id,
            "pages_ingested": n,
            "output_file": output_path.name,
            "output_path": str(output_path),
            "total_pages": pipeline.total_pages,
        }
    except Exception as e:
        logger.exception("Run pipeline failed")
        raise HTTPException(500, str(e))


@app.get("/status")
async def status(project_id: str = "default"):
    """Get retriever / index status."""
    pipeline = _get_pipeline(project_id)
    return {
        "ok": True,
        "project_id": project_id,
        "retriever_mode": "lexical",
        "pages_indexed": pipeline.total_pages,
    }


@app.get("/download/{project_id}")
async def download(project_id: str):
    """Download the generated synthesis PDF."""
    output_path = OUTPUT_DIR / f"synthesis_input_{project_id}.pdf"
    if not output_path.exists():
        raise HTTPException(404, f"Output not found for project '{project_id}'")

    return FileResponse(
        str(output_path),
        media_type="application/pdf",
        filename=output_path.name,
    )


@app.post("/reset")
async def reset(project_id: str = "default"):
    """Reset project: clear index and screenshots."""
    pipeline = _get_pipeline(project_id)
    pipeline.reset()
    return {"ok": True, "project_id": project_id}


@app.post("/clear-reset")
async def clear_reset(project_id: str = "default"):
    """Safe per-project reset for an OneDrive-synced workspace.

    Only derived state is cleared: the page-text index, the screenshot cache,
    and the generated synthesis PDF. The user's dossier files inside the
    project folder (PROJECT_ROOT/<project_id>/) are NEVER touched — they are
    the source of truth and live in a synced directory, so deleting them would
    be destructive and surprising.
    """
    cleared = 0

    # 1) Page-text index.
    if index_exists(project_id):
        try:
            delete_index(project_id)
            cleared += 1
        except OSError as e:
            logger.warning(f"Could not delete index for {project_id}: {e}")

    # 2) Screenshot cache (global, not inside the project folder).
    if SCREENSHOTS_DIR.exists():
        for rt in REPORT_TYPES:
            rt_dir = SCREENSHOTS_DIR / rt
            if not rt_dir.exists():
                continue
            for item in list(rt_dir.iterdir()):
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                        cleared += 1
                    elif item.is_dir():
                        shutil.rmtree(item)
                        cleared += 1
                except OSError as e:
                    logger.warning(f"Could not remove {item}: {e}")

    # 3) Generated synthesis PDF.
    out_pdf = OUTPUT_DIR / f"synthesis_input_{project_id}.pdf"
    if out_pdf.exists():
        try:
            out_pdf.unlink()
            cleared += 1
        except OSError as e:
            logger.warning(f"Could not delete {out_pdf}: {e}")

    logger.info(
        f"Safe reset for '{project_id}': cleared {cleared} derived item(s) "
        f"(index + screenshots + output PDF). Dossier files untouched."
    )
    return {"ok": True, "project_id": project_id, "cleared": cleared}


# ---------------------------------------------------------------------------
# Auto-classification endpoints (no upload — dossiers are read from the
# per-project folder PROJECT_ROOT/<project_id>/)
# ---------------------------------------------------------------------------

@app.post("/classify")
async def classify_inbox(project_id: str = "default"):
    """Classify every dossier in the project folder.

    High-confidence matches are auto-archived into
    PROJECT_ROOT/<project_id>/{type}/. Low-confidence / UNKNOWN files stay in
    the project folder, flagged for review.

    Returns:
        {"ok": true, "results": [ {filename, report_type, confidence,
          scores, low_confidence, archived, current_path}, ... ]}
    """
    pipeline = _get_pipeline(project_id)
    base_dir = project_data_dir(project_id)
    classifier = Classifier(base_dir=base_dir)
    try:
        out = classifier.classify_inbox()
        return {
            "ok": True,
            "results": out["results"],
            "unprocessed": out.get("unprocessed", []),
        }
    except Exception as e:
        logger.exception("Classification failed")
        raise HTTPException(500, str(e))


@app.post("/classify/confirm")
async def confirm_classification(req: ClassifyConfirmRequest, project_id: str = "default"):
    """Apply final type decisions, moving files into <project>/{type}/.

    Query param:
        project_id: str  (the project folder name)
    Body (JSON):
        decisions: [ {"filename": "...", "report_type": "CLINICAL"}, ... ]

    Already-correct files are skipped; low-confidence and corrected files
    are moved to their chosen folder.
    """
    pipeline = _get_pipeline(project_id)
    base_dir = project_data_dir(project_id)
    classifier = Classifier(base_dir=base_dir)
    try:
        moved = classifier.apply_decisions(req.decisions)
        return {"ok": True, "moved": moved, "count": len(moved)}
    except Exception as e:
        logger.exception("Confirm classification failed")
        raise HTTPException(500, str(e))


@app.get("/classify/profiles")
async def get_classify_profiles():
    """Read current type profiles from classify/*.txt."""
    try:
        profiles = load_classify_profiles()
        return {"ok": True, "profiles": profiles}
    except Exception as e:
        logger.exception("Failed to load classify profiles")
        raise HTTPException(500, str(e))


@app.post("/classify/profiles/save")
async def save_classify_profiles_endpoint(req: ClassifyProfileSaveRequest):
    """Save type profiles to classify/*.txt.

    Body (JSON):
        profiles: {"CLINICAL": "...", "FE": "...", "CE": "..."}
    """
    try:
        for rt in req.profiles:
            if rt not in REPORT_TYPES:
                raise HTTPException(
                    400,
                    f"Invalid report_type '{rt}'. Must be one of {REPORT_TYPES}",
                )
        save_classify_profiles(req.profiles)
        return {"ok": True, "saved": list(req.profiles.keys())}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to save classify profiles")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Query management endpoints
# ---------------------------------------------------------------------------

@app.get("/queries")
async def get_queries():
    """Read current per-type query texts from queries/*.txt files.

    Returns:
        {"CLINICAL": "...", "FE": "...", "CE": "..."}
    """
    try:
        queries = load_queries_from_files()
        return {"ok": True, "queries": queries}
    except Exception as e:
        logger.exception("Failed to load queries")
        raise HTTPException(500, str(e))


@app.post("/queries/save")
async def save_queries(req: QueriesSaveRequest):
    """Save per-type query texts to queries/*.txt files.

    Body (JSON):
        queries: {"CLINICAL": "...", "FE": "...", "CE": "..."}
    """
    try:
        # Validate that all keys are known report types
        for rt in req.queries:
            if rt not in REPORT_TYPES:
                raise HTTPException(
                    400,
                    f"Invalid report_type '{rt}'. Must be one of {REPORT_TYPES}",
                )
        save_queries_to_files(req.queries)
        return {"ok": True, "saved": list(req.queries.keys())}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to save queries")
        raise HTTPException(500, str(e))

# ---------------------------------------------------------------------------

