"""
PDF generation module — reportlab

Produces a single synthesis-input PDF for a downstream multimodal LLM.
Each selected page becomes ONE page containing a short structured
annotation (source / type / key terms) plus a high-resolution screenshot.
The raw page text is deliberately NOT embedded — the screenshot is the
authoritative visual and the raw extraction only adds noise (diagram
fragments, numbering, leaked paths) that distracts the LLM.
"""

from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

# CJK-capable font so the target-formula banner renders Chinese correctly
# (Helvetica cannot). Falls back to Helvetica-Bold if the CID font is
# unavailable (Chinese would then be dropped from the banner).
_CJK_FONT = "STSong-Light"
try:
    pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
    _CJK_AVAILABLE = True
except Exception as e:  # pragma: no cover - environment without CJK CMaps
    logger.warning(f"CJK font '{_CJK_FONT}' unavailable, Chinese may not render: {e}")
    _CJK_AVAILABLE = False

from .config import (
    OUTPUT_DIR,
    REPORT_TYPES,
    REPORT_TYPE_LABELS,
    PROJECT_ROOT,
    TARGET_FORMULA_BANNER_TEMPLATE,
)
from .logger import get_logger

logger = get_logger(__name__)

# --- Page dimensions ---
PAGE_W, PAGE_H = landscape(A4)
MARGIN = 20 * mm
CONTENT_W = PAGE_W - 2 * MARGIN


class PDFGenerator:
    """Generates the final synthesis input PDF from summary-page data."""

    def __init__(self):
        self._styles = self._build_styles()

    def _build_styles(self) -> dict:
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "SynTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            spaceAfter=14,
        )
        heading_style = ParagraphStyle(
            "SynHeading",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            spaceBefore=20,
            spaceAfter=8,
            textColor="#1a1a2e",
        )
        sub_heading_style = ParagraphStyle(
            "SynSubHeading",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=12,
            spaceBefore=14,
            spaceAfter=6,
            textColor="#16213e",
        )
        body_style = ParagraphStyle(
            "SynBody",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            spaceAfter=6,
        )
        meta_style = ParagraphStyle(
            "SynMeta",
            parent=styles["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=8,
            textColor="#888888",
        )
        return {
            "title": title_style,
            "heading": heading_style,
            "sub_heading": sub_heading_style,
            "body": body_style,
            "meta": meta_style,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        project_id: str,
        summary_pages: list[dict],
        project_owner: str = "",
        target_formula: str = "",
    ) -> Path:
        """Build the synthesis PDF.

        Args:
            project_id: e.g. "PROJ-001"
            summary_pages: list of dicts:
                {
                    "text": str,
                    "screenshot": Path | str,
                    "filename": str,
                    "report_type": str ("CLINICAL" / "FE" / "CE"),
                    "page_label": int,
                    "source_path": str,
                }
            target_formula: user-supplied final target formula. When set, it
                is rendered as a highlighted banner on the cover (metadata
                injection) to anchor the downstream AI.

        Returns:
            Path to the generated PDF.
        """
        output_path = OUTPUT_DIR / f"synthesis_input_{project_id}.pdf"

        # Footer text is drawn per page via the onPage callback. We collect
        # one entry per physical page (cover + section headers + each report
        # page) in the same order the story is built. Empty string = no footer.
        page_footers: list[str] = [""]  # cover

        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=landscape(A4),
            rightMargin=MARGIN,
            leftMargin=MARGIN,
            topMargin=MARGIN,
            bottomMargin=MARGIN,
        )
        story = []

        # --- Cover page ---
        self._add_cover(
            story, project_id, summary_pages, project_owner, target_formula
        )
        # cover already ends with a PageBreak

        # --- Group by report_type, then sort by filename ---
        groups = self._group_and_sort(summary_pages)

        for report_type in REPORT_TYPES:
            items = groups.get(report_type, [])
            if not items:
                continue
            story.append(Paragraph(
                f"Section: {report_type} Reports",
                self._styles["heading"],
            ))
            story.append(Spacer(1, 4 * mm))
            story.append(PageBreak())  # section header on its own page
            page_footers.append("")    # section header page has no footer

            for idx, item in enumerate(items, 1):
                footer = self._add_report_page(story, item, idx)
                page_footers.append(footer)
                story.append(PageBreak())

        doc.build(
            story,
            onFirstPage=lambda canvas, doc: self._draw_footer(canvas, doc, page_footers),
            onLaterPages=lambda canvas, doc: self._draw_footer(canvas, doc, page_footers),
        )
        logger.info(
            f"PDF generated: {output_path} "
            f"({len(summary_pages)} summary pages)"
        )
        return output_path

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _add_cover(
        self,
        story: list,
        project_id: str,
        summary_pages: list[dict],
        project_owner: str = "",
        target_formula: str = "",
    ):
        story.append(Paragraph("Synthesis Input", self._styles["title"]))
        story.append(Paragraph(
            f"Project: {project_id}",
            self._styles["sub_heading"],
        ))
        if project_owner:
            story.append(Paragraph(
                f"Project Owner: {project_owner}",
                self._styles["sub_heading"],
            ))
        story.append(Spacer(1, 6 * mm))

        # --- Metadata injection: target-formula banner ------------------
        # Anchors the downstream AI on the final target formula so it does
        # not drift across the multiple formulas present in the dossiers.
        if target_formula:
            banner_text = TARGET_FORMULA_BANNER_TEMPLATE.format(
                formula=target_formula
            )
            banner_style = ParagraphStyle(
                "SynBanner",
                parent=self._styles["body"],
                fontName=_CJK_FONT if _CJK_AVAILABLE else "Helvetica-Bold",
                fontSize=11,
                leading=15,
                textColor="#7a1f1f",
            )
            banner_tbl = Table(
                [[Paragraph(banner_text, banner_style)]],
                colWidths=[CONTENT_W],
            )
            banner_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), "#fdeaea"),
                ("BOX", (0, 0), (-1, -1), 0.75, "#c0392b"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(banner_tbl)
            story.append(Spacer(1, 6 * mm))

        # Count by type
        type_counts = Counter(
            p.get("report_type", "?") for p in summary_pages
        )
        meta_parts = [f"Total summary pages: {len(summary_pages)}"]
        for rt in REPORT_TYPES:
            if type_counts.get(rt):
                meta_parts.append(f"{rt}: {type_counts[rt]}")
        meta_parts.append(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        story.append(Paragraph(" | ".join(meta_parts), self._styles["meta"]))
        story.append(Spacer(1, 6 * mm))

        # --- Table of contents (guides downstream AI to map every #N page
        # back to its source file / type / source page) -------------------
        self._add_toc(story, summary_pages)
        story.append(PageBreak())

    def _add_toc(self, story: list, summary_pages: list[dict]):
        """Render a cover Table of Contents mapping every selected page back
        to its origin, so the downstream AI can locate and cite each page.

        Two-column layout keeps the TOC on the cover even for large dossiers.
        Columns per side: #N (per-type index, matching the page footer),
        Source File (no extension), Type (CLINICAL/FE/CE), Source Page.
        """
        groups = self._group_and_sort(summary_pages)
        rows = []
        for rt in REPORT_TYPES:
            items = groups.get(rt, [])
            for i, item in enumerate(items, 1):
                raw_name = item.get("filename", "unknown")
                src = Path(raw_name).stem or raw_name
                rows.append([str(i), src, rt, str(item.get("page_label", "?"))])

        # CJK-capable style for the (possibly Chinese) source-file column
        src_style = ParagraphStyle(
            "TocSrc",
            parent=self._styles["body"],
            fontName=_CJK_FONT if _CJK_AVAILABLE else "Helvetica",
            fontSize=8,
            leading=9.5,
            spaceAfter=0,
        )
        header = ["#N", "Source File", "Type", "Source Page"]
        sub_widths = [14 * mm, 70 * mm, 22 * mm, 18 * mm]

        def _build_sub(chunk: list):
            data = [header]
            for row in chunk:
                data.append([row[0], Paragraph(row[1], src_style), row[2], row[3]])
            t = Table(data, colWidths=sub_widths, repeatRows=1)
            style = [
                ("GRID", (0, 0), (-1, -1), 0.4, "#d0d0d0"),
                ("BACKGROUND", (0, 0), (-1, 0), "#2c3e50"),
                ("TEXTCOLOR", (0, 0), (-1, 0), "#ffffff"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (2, 0), (3, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 1.0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.0),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
            for r in range(1, len(data)):
                if r % 2 == 0:
                    style.append(("BACKGROUND", (0, r), (-1, r), "#f4f6f8"))
            t.setStyle(TableStyle(style))
            return t

        half = (len(rows) + 1) // 2
        left_t = _build_sub(rows[:half])
        right_t = _build_sub(rows[half:])

        outer = Table([[left_t, right_t]], colWidths=[124 * mm, 124 * mm])
        outer.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEAFTER", (0, 0), (0, 0), 0.4, "#d0d0d0"),
        ]))

        story.append(Paragraph(
            "Table of Contents / 目录",
            ParagraphStyle(
                "TocHeading",
                parent=self._styles["heading"],
                spaceBefore=0,
                spaceAfter=4,
            ),
        ))
        story.append(Paragraph(
            "Each entry is one <b>independent evidence page</b> in this "
            "dossier. The <b>#N</b> index matches that page's footer — treat "
            "every page as a separate evidence item and cite it "
            "independently; never merge or skip a page.",
            self._styles["body"],
        ))
        story.append(Spacer(1, 3 * mm))
        story.append(outer)

    def _add_report_page(
        self,
        story: list,
        item: dict,
        index: int,
    ) -> str:
        """Append the screenshot for one report page to the story.

        The output PDF is landscape (A4 landscape) because the source
        material users upload (PDF/PPT) is landscape, so the screenshot
        fills the full page width and stays crisp. The one-line annotation
        is returned so the caller can draw it at the bottom of the page
        via the onPage callback.
        """
        filename = item.get("filename", "unknown.pdf")
        report_type = item.get("report_type", "?")
        page_label = item.get("page_label", "?")
        source_rel = self._relative_source(item.get("source_path", ""))
        matched = item.get("matched_terms", []) or []
        key_terms = ", ".join(matched[:6]) if matched else "—"

        footer_text = (
            f"#{index}  [{report_type}]  {filename}  —  Page {page_label}"
            f" | Source: {source_rel} | Key terms: {key_terms}"
        )

        content_h = PAGE_H - 2 * MARGIN
        ann_h = 6 * mm          # reserved height at the bottom for the footer
        max_img_h = content_h - ann_h

        screenshot = item.get("screenshot")
        ss_path = Path(screenshot) if screenshot else None

        if ss_path and ss_path.exists():
            try:
                with Image.open(ss_path) as img:
                    w, h = img.size

                scale = min(CONTENT_W / w, max_img_h / h) if (w and h) else 1.0
                img_w, img_h = w * scale, h * scale

                rl_img = RLImage(str(ss_path), width=img_w, height=img_h)
                img_table = Table(
                    [[rl_img]],
                    colWidths=[CONTENT_W],
                    rowHeights=[max_img_h],
                )
                img_table.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]))
                story.append(img_table)
            except Exception as e:
                logger.error(f"Failed to embed screenshot {ss_path}: {e}")
                story.append(Paragraph(
                    f"[Screenshot unavailable: {e}]",
                    self._styles["meta"],
                ))
        else:
            story.append(Paragraph(
                "[Screenshot unavailable]",
                self._styles["meta"],
            ))

        return footer_text

    def _draw_footer(
        self,
        canvas,
        doc,
        page_footers: list[str],
    ):
        """Draw the single-line annotation at the bottom of a report page.

        The cover and section headers have empty footer entries, so nothing
        is drawn for them. Long footers are truncated to fit one line.
        """
        idx = doc.page - 1
        if idx < 0 or idx >= len(page_footers):
            return
        text = page_footers[idx]
        if not text:
            return

        text = self._truncate_to_width(text, size=7)
        canvas.saveState()
        canvas.setFont("Helvetica-Oblique", 7)
        canvas.setFillColor("#888888")
        canvas.drawString(MARGIN, MARGIN + 2 * mm, text)
        canvas.restoreState()

    def _truncate_to_width(
        self,
        text: str,
        font: str = "Helvetica-Oblique",
        size: int = 7,
        max_width: float = CONTENT_W,
    ) -> str:
        """Truncate a string so it fits in one line at the given font/size."""
        if stringWidth(text, font, size) <= max_width:
            return text
        while text and stringWidth(text + "...", font, size) > max_width:
            text = text[:-1]
        return text + "..."

    def _relative_source(self, source: str) -> str:
        """Return a clean project-relative path (e.g. data/CLINICAL/xxx.pdf).

        Avoids leaking the absolute local filesystem path into the output.
        """
        if not source:
            return ""
        try:
            rel = Path(source).resolve().relative_to(PROJECT_ROOT.resolve())
            return rel.as_posix()
        except ValueError:
            return Path(source).name

    def _group_and_sort(
        self,
        summary_pages: list[dict],
    ) -> dict[str, list[dict]]:
        """Group pages by report_type and sort alphabetically by filename."""
        groups: dict[str, list[dict]] = {rt: [] for rt in REPORT_TYPES}
        for page in summary_pages:
            rt = page.get("report_type", "UNKNOWN")
            if rt in groups:
                groups[rt].append(page)

        for rt in groups:
            groups[rt].sort(key=lambda p: p.get("filename", "").lower())

        return groups
