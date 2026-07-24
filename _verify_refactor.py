"""End-to-end verification of the per-project refactor.

Creates a synthetic project folder with 3 keyword-rich PDFs, then drives the
real API (via FastAPI TestClient) through:
    POST /project/scan   -> lists dossiers
    POST /classify       -> moves files into PROJECT_ROOT/<name>/{CLINICAL,FE,CE}/
    POST /run            -> ingest + package -> synthesis PDF
Then asserts the filesystem state and cleans up.
"""
import shutil
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from fastapi.testclient import TestClient

import src.config as cfg
from src.api import app

ROOT = cfg.PROJECT_ROOT
PROJ = "PROJ-DEMO"
PROJ_DIR = cfg.project_data_dir(PROJ)
OUT_PDF = cfg.OUTPUT_DIR / f"synthesis_input_{PROJ}.pdf"
IDX = cfg.INDEX_DIR / f"{PROJ}.json"


CLINICAL_TXT = (
    "Clinical study report. This clinical trial investigated dermatological "
    "efficacy and tolerance under controlled conditions. Safety was assessed "
    "via grader assessment and instrumental clinical measurement. Endpoints "
    "included adverse event monitoring before/after treatment."
)
SENSORY_TXT = (
    "Sensory evaluation report. A sensory panel performed sensory analysis "
    "and descriptive analysis of texture, fragrance, odor, appearance and "
    "feel. Hedonic liking and sensory attributes were scored by trained "
    "panelists; touch and color were evaluated."
)
CONSUMER_TXT = (
    "Consumer evaluation report. A consumer test (consumer study, consumer "
    "panel) used self-assessment, usage test and home-use test with a "
    "questionnaire. Satisfaction, perception and consumer feedback supported "
    "claim substantiation among panelists."
)


def make_pdf(name: str, text: str):
    path = PROJ_DIR / name
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 11)
    y = 800
    for line in text.split(". "):
        c.drawString(50, y, line.strip() + ".")
        y -= 16
    c.showPage()
    c.save()
    return path


def main():
    # --- setup ---
    shutil.rmtree(PROJ_DIR, ignore_errors=True)
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    make_pdf("clinical_A.pdf", CLINICAL_TXT)
    make_pdf("sensory_B.pdf", SENSORY_TXT)
    make_pdf("consumer_C.pdf", CONSUMER_TXT)
    print(f"[setup] created {PROJ_DIR} with 3 PDFs")

    client = TestClient(app)

    # 1) scan
    r = client.post("/project/scan", json={"project_name": PROJ})
    print("[scan]", r.status_code, r.json().get("count"), "files")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3, r.json()
    assert len(r.json()["files"]) == 3

    # 2) classify
    r = client.post(f"/classify?project_id={PROJ}")
    print("[classify]", r.status_code, "results:", [(x["filename"], x["report_type"], x["archived"]) for x in r.json().get("results", [])])
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) == 3, results
    # confirm & sort (one click)
    decisions = [{"filename": x["filename"], "report_type": x["report_type"]} for x in results]
    r2 = client.post(f"/classify/confirm?project_id={PROJ}", json={"decisions": decisions})
    print("[confirm]", r2.status_code, "moved:", r2.json().get("count"))
    assert r2.status_code == 200 and r2.json()["count"] == 3, r2.text

    # filesystem: each type folder should now contain exactly 1 PDF
    for rt in cfg.REPORT_TYPES:
        pdfs = sorted((PROJ_DIR / rt).glob("*.pdf"))
        print(f"   {rt}/ -> {[p.name for p in pdfs]}")
        assert len(pdfs) == 1, f"expected 1 pdf in {rt}, got {pdfs}"
    # top-level should have no PDFs left (all moved)
    top_pdfs = sorted(PROJ_DIR.glob("*.pdf"))
    print("   top-level pdfs:", [p.name for p in top_pdfs])
    assert not top_pdfs, f"top-level still has pdfs: {top_pdfs}"

    # 3) run (ingest + package)
    r = client.post("/run", json={"project_id": PROJ, "top_n": -1})
    print("[run]", r.status_code, r.json().get("output_file"), "pages:", r.json().get("pages_ingested"))
    assert r.status_code == 200, r.text
    assert r.json()["pages_ingested"] > 0, r.json()
    assert OUT_PDF.exists() and OUT_PDF.stat().st_size > 0, "output PDF missing"

    # 4) safe reset must NOT delete dossier files
    r = client.post(f"/clear-reset?project_id={PROJ}")
    print("[clear-reset]", r.status_code, "cleared:", r.json().get("cleared"))
    assert r.status_code == 200
    # dossier files still present after reset
    remaining = sum(len(list((PROJ_DIR / rt).glob("*.pdf"))) for rt in cfg.REPORT_TYPES)
    print("   dossier pdfs after reset:", remaining)
    assert remaining == 3, "reset deleted user dossier files!"
    assert not OUT_PDF.exists(), "output PDF not cleared by reset"

    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    try:
        main()
    finally:
        # cleanup
        shutil.rmtree(PROJ_DIR, ignore_errors=True)
        for rt in cfg.REPORT_TYPES:
            d = cfg.SCREENSHOTS_DIR / rt
            if d.exists():
                for it in list(d.iterdir()):
                    try:
                        if it.is_file():
                            it.unlink()
                        elif it.is_dir():
                            shutil.rmtree(it)
                    except OSError:
                        pass
        for f in (OUT_PDF, IDX):
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                pass
        print("[cleanup] removed synthetic PROJ-DEMO + derived artifacts")
