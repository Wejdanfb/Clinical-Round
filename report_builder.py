"""
Clinical Round report builder.

Populates the official blank template (assets/clinical_round_template.docx)
by editing its WordprocessingML directly. Everything that is not report
content - page setup, header logo, footer graphics, title, metadata labels,
blue/grey shading, borders, column widths, fonts - is carried over from the
template untouched.

How elements are located (no fixed indexes):
  * The observations table is the table containing a row whose cells read
    الملاحظات / توثيقها ( صور ) / التوصيات / هل الملاحظات مكررة /
    الخطة التصحيحية / التوثيق.  Column order is read from that row.
  * Metadata values are written after their Arabic labels
    (اسم المنشأة: / القسم: / التاريخ: / عدد التمريض: / عدد المرضى: /
    فريق دعم الجولات الإدارية :).
  * Observation rows are cloned from the template's own empty rows:
      - "standard" prototype: first row below the column header with one
        cell per column (6 cells);
      - "wide" prototype: the template's row whose first cell spans the
        observation + evidence columns (gridSpan=2).  Used only when the
        user asks for a text-only wide row, mirroring how the completed
        sample report uses it.

Conventions taken from the completed sample report:
  * each line of user text becomes its own paragraph inside the cell;
  * Arabic runs carry <w:rtl/> and the complex-script font hint, Latin
    runs do not (mixed text such as "الجراحة 2A" stays in the right order);
  * observation text is black (text1), not bold; recommendation text is not
    bold; the repeated flag is bold, centred, written as نعم / لا;
  * photographs are inline pictures inside the evidence cell, about 1.9 in
    wide, with aspect ratio preserved;
  * an observation without photographs shows "لايوجد" in the evidence cell;
  * the date is written as "21 اكتوبر 2026".
"""

from __future__ import annotations

import copy
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from lxml import etree
from PIL import Image, ImageOps, UnidentifiedImageError

# --------------------------------------------------------------------------
# Namespaces
# --------------------------------------------------------------------------
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

W = "{%s}" % W_NS


def w(tag: str) -> str:
    return W + tag


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class TemplateError(Exception):
    """The Word template is missing, corrupted or not the expected form."""


class ReportInputError(Exception):
    """User-supplied data is invalid (message is safe to show to the user)."""


# --------------------------------------------------------------------------
# Limits / layout constants
# --------------------------------------------------------------------------
EMU_PER_INCH = 914400
EMU_PER_TWIP = 635

MAX_IMAGE_BYTES = 15 * 1024 * 1024        # per uploaded photo
MAX_IMAGE_PIXELS = 60_000_000             # decompression-bomb guard
MAX_STORED_SIDE_PX = 1600                 # photos are downscaled to this
SINGLE_PHOTO_MAX_H_IN = 2.6               # sample report photos: 2.3-2.6 in tall
MULTI_PHOTO_MAX_H_IN = 2.1                # several photos stacked in one cell
PHOTO_SAFETY_MARGIN_IN = 0.08             # keep pictures clear of cell borders

# Rows whose estimated height is below this are kept on one page
# (w:cantSplit). Taller rows are allowed to break so nothing is clipped.
KEEP_TOGETHER_MAX_IN = 4.6

AR_MONTHS = ["يناير", "فبراير", "مارس", "ابريل", "مايو", "يونيو",
             "يوليو", "اغسطس", "سبتمبر", "اكتوبر", "نوفمبر", "ديسمبر"]
REPEATED_AR = {"yes": "نعم", "no": "لا"}
NO_EVIDENCE_TEXT = "لايوجد"

FONT = "Janna LT"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Photo:
    data: bytes
    ext: str            # "jpeg" or "png"
    width_px: int
    height_px: int


@dataclass
class Observation:
    text: str = ""
    recommendations: str = ""
    repeated: str = ""          # "yes" / "no"
    corrective_plan: str = ""
    documentation: str = ""
    photos: List[Photo] = field(default_factory=list)
    wide: bool = False          # text-only row spanning observation+evidence


@dataclass
class ReportData:
    facility: str = ""
    department: str = ""
    round_date: Optional[date] = None
    nurses: str = ""
    patients: str = ""
    support_team: str = ""
    hospital_action: str = ""
    observations: List[Observation] = field(default_factory=list)
    repeat_header: bool = False


# --------------------------------------------------------------------------
# Image processing
# --------------------------------------------------------------------------
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def process_image(raw: bytes, display_name: str = "photo") -> Photo:
    """Validate an uploaded photo and return a cleaned copy.

    * only real JPEG / PNG content is accepted (checked by signature and by
      decoding the whole image, so truncated/corrupted files are rejected);
    * camera orientation (EXIF) is applied, then all metadata (EXIF, GPS,
      etc.) is dropped;
    * large photos are downscaled so the .docx stays a reasonable size.
    """
    name = display_name or "photo"
    if not raw:
        raise ReportInputError(f"The photo “{name}” is empty.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ReportInputError(
            f"The photo “{name}” is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    if not (raw.startswith(_JPEG_MAGIC) or raw.startswith(_PNG_MAGIC)):
        raise ReportInputError(
            f"“{name}” is not a JPEG or PNG image. Only .jpg, .jpeg and .png photos are supported.")

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            w_px, h_px = probe.size
            if fmt not in ("JPEG", "PNG"):
                raise ReportInputError(f"“{name}” is not a JPEG or PNG image.")
            if w_px * h_px > MAX_IMAGE_PIXELS:
                raise ReportInputError(f"The photo “{name}” has too many pixels to process.")
            probe.verify()                      # structural check
        with Image.open(io.BytesIO(raw)) as im:
            im.load()                           # full decode: catches truncation
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_STORED_SIDE_PX, MAX_STORED_SIDE_PX), Image.LANCZOS)
            out = io.BytesIO()
            if fmt == "JPEG":
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(out, "JPEG", quality=85, optimize=True)
                ext = "jpeg"
            else:
                has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
                if has_alpha:
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                elif im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(out, "PNG", optimize=True)
                ext = "png"
                if out.tell() > 2_500_000:      # photo saved as PNG: use JPEG
                    out = io.BytesIO()
                    im.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
                    ext = "jpeg"
            return Photo(out.getvalue(), ext, im.size[0], im.size[1])
    except ReportInputError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ReportInputError(f"The photo “{name}” is damaged or could not be read.")


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
_ARABIC_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                  (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def _is_arabic(ch: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in _ARABIC_RANGES)


def _char_class(ch: str) -> Optional[str]:
    if _is_arabic(ch):
        return "ar"
    if unicodedata.category(ch).startswith("L"):
        return "lat"
    return None  # digits, spaces, punctuation: neutral


def split_by_script(text: str) -> List[Tuple[str, str]]:
    """Split text into (segment, 'ar'|'lat') runs, as Word stores mixed
    Arabic/Latin text in the sample report."""
    if not text:
        return []
    first_strong = next((c for c in (_char_class(ch) for ch in text) if c), "ar")
    segs: List[List[str]] = []
    cur_cls = first_strong
    buf: List[str] = []
    for ch in text:
        cls = _char_class(ch)
        if cls is None or cls == cur_cls:
            buf.append(ch)
            continue
        if buf:
            segs.append(["".join(buf), cur_cls])
        buf, cur_cls = [ch], cls
    if buf:
        segs.append(["".join(buf), cur_cls])
    return [(s, c) for s, c in segs]


def clean_text(value) -> str:
    if value is None:
        return ""
    s = str(value).replace("\r\n", "\n").replace("\r", "\n")
    # strip characters that are illegal in XML 1.0
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]", "", s)
    return s


def text_lines(value: str) -> List[str]:
    """User text -> list of paragraph texts (line breaks preserved,
    leading/trailing blank lines dropped)."""
    s = clean_text(value)
    lines = [ln.rstrip() for ln in s.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def arabic_date(d: date) -> str:
    return f"{d.day} {AR_MONTHS[d.month - 1]} {d.year}"


_INVALID_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(department: str, round_date: Optional[date]) -> str:
    """Clinical_Round_<Department>_<YYYY-MM-DD>.docx, safe on Windows.

    Only the Latin letters / digits of the department are used (e.g.
    "الجراحة 2A" -> "2A"): browsers silently replace non-ASCII download
    names with "download", so an ASCII name is the reliable choice."""
    dept = _INVALID_FN.sub("", clean_text(department))
    dept = re.sub(r"[^A-Za-z0-9 _-]", " ", dept).strip()
    dept = re.sub(r"\s+", "_", dept)
    dept = re.sub(r"_+", "_", dept).strip("._ ")[:60]
    parts = ["Clinical_Round"]
    if dept:
        parts.append(dept)
    if round_date:
        parts.append(round_date.isoformat())
    return "_".join(parts) + ".docx"


# --------------------------------------------------------------------------
# Run / paragraph construction
# --------------------------------------------------------------------------
def _rpr(script: str, bold=False, color_text1=False, no_proof=False, lang_ar=False):
    rpr = etree.Element(w("rPr"))
    fonts = etree.SubElement(rpr, w("rFonts"))
    fonts.set(w("ascii"), FONT)
    fonts.set(w("hAnsi"), FONT)
    fonts.set(w("cs"), FONT)
    if script == "ar":
        fonts.set(w("hint"), "cs")
    if bold:
        etree.SubElement(rpr, w("b"))
        etree.SubElement(rpr, w("bCs"))
    if no_proof:
        etree.SubElement(rpr, w("noProof"))
    if color_text1:
        c = etree.SubElement(rpr, w("color"))
        c.set(w("val"), "000000")
        c.set(w("themeColor"), "text1")
    if script == "ar":
        etree.SubElement(rpr, w("rtl"))
    if lang_ar:
        lg = etree.SubElement(rpr, w("lang"))
        lg.set(w("val"), "ar-SA")
    return rpr


def make_runs(text: str, **style) -> List[etree._Element]:
    runs = []
    for seg, script in split_by_script(text):
        r = etree.Element(w("r"))
        r.append(_rpr(script, **style))
        t = etree.SubElement(r, w("t"))
        t.text = seg
        if seg != seg.strip() or "  " in seg:
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        runs.append(r)
    return runs


def _proto_ppr(tc) -> Optional[etree._Element]:
    p = tc.find(w("p"))
    if p is None:
        return None
    ppr = p.find(w("pPr"))
    return copy.deepcopy(ppr) if ppr is not None else None


def _new_paragraph(ppr_proto) -> etree._Element:
    p = etree.Element(w("p"))
    if ppr_proto is not None:
        p.append(copy.deepcopy(ppr_proto))
    else:
        ppr = etree.SubElement(p, w("pPr"))
        etree.SubElement(ppr, w("bidi"))
    return p


def _clear_cell(tc) -> Optional[etree._Element]:
    """Remove all block content from a cell, return the first paragraph's
    pPr (the template's formatting for that cell)."""
    ppr = _proto_ppr(tc)
    for child in list(tc):
        if child.tag != w("tcPr"):
            tc.remove(child)
    return ppr


def fill_cell_text(tc, value: str, **style) -> int:
    """Write text into a table cell, one paragraph per line. Returns the
    number of paragraphs written."""
    ppr = _clear_cell(tc)
    lines = text_lines(value)
    if not lines:
        tc.append(_new_paragraph(ppr))
        return 0
    for line in lines:
        p = _new_paragraph(ppr)
        for r in make_runs(line, **style):
            p.append(r)
        tc.append(p)
    return len(lines)


# --------------------------------------------------------------------------
# Template analysis
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(w("t")))


HEADER_MATCHERS = {
    "observation": lambda t: t == "الملاحظات",
    "evidence": lambda t: "توثيقها" in t or "صور" in t,
    "recommendations": lambda t: t.startswith("التوصيات"),
    "repeated": lambda t: "مكررة" in t,
    "corrective_plan": lambda t: t == "الخطة التصحيحية",
    "documentation": lambda t: t == "التوثيق",
}

META_LABELS = {
    "facility": "اسم المنشأة",
    "department": "القسم",
    "date": "التاريخ",
    "nurses": "عدد التمريض",
    "patients": "عدد المرضى",
    "support_team": "فريق دعم الجولات",
}
HOSPITAL_ACTION_LABEL = "الخطة التصحيحية المتخذة"


def _grid_layout(tr) -> List[Tuple[int, int, etree._Element]]:
    """[(grid_start, span, tc), ...] for a row."""
    out, col = [], 0
    trpr = tr.find(w("trPr"))
    if trpr is not None:
        gb = trpr.find(w("gridBefore"))
        if gb is not None:
            col += int(gb.get(w("val"), "0"))
    for tc in tr.findall(w("tc")):
        span = 1
        tcpr = tc.find(w("tcPr"))
        if tcpr is not None:
            gs = tcpr.find(w("gridSpan"))
            if gs is not None:
                span = int(gs.get(w("val"), "1"))
        out.append((col, span, tc))
        col += span
    return out


@dataclass
class TemplateMap:
    table: etree._Element
    header_row: etree._Element
    header_index: int
    field_cols: Dict[str, int]              # field -> grid column
    grid_widths: List[int]                  # twips
    standard_proto: etree._Element
    wide_proto: Optional[etree._Element]
    meta_paragraphs: Dict[str, etree._Element]
    hospital_action_paragraph: Optional[etree._Element]
    cell_margin_twips: int


def analyze_template(doc_root, styles_root=None) -> TemplateMap:
    body = doc_root.find(w("body"))
    if body is None:
        raise TemplateError("The template has no document body.")

    for tbl in body.iter(w("tbl")):
        rows = tbl.findall(w("tr"))
        for idx, tr in enumerate(rows):
            layout = _grid_layout(tr)
            found = {}
            for start, span, tc in layout:
                t = _norm(_text(tc))
                for fld, match in HEADER_MATCHERS.items():
                    if fld not in found and t and match(t):
                        found[fld] = start
                        break
            if len(found) != len(HEADER_MATCHERS):
                continue

            # ---- header row located ----
            n_cells = len(layout)
            standard = wide = None
            for tr2 in rows[idx + 1:]:
                lay2 = _grid_layout(tr2)
                spans = [s for _, s, _ in lay2]
                if standard is None and len(lay2) == n_cells and all(s == 1 for s in spans):
                    standard = tr2
                if wide is None and lay2 and lay2[0][1] > 1:
                    # first cell must cover observation + evidence columns
                    st, sp, _ = lay2[0]
                    cols = set(range(st, st + sp))
                    if found["observation"] in cols and found["evidence"] in cols:
                        wide = tr2
            if standard is None:
                raise TemplateError("The template has no empty observation row to copy.")

            grid = [int(gc.get(w("w"), "0")) for gc in tbl.find(w("tblGrid")).findall(w("gridCol"))]

            meta: Dict[str, etree._Element] = {}
            hosp = None
            for tr0 in rows[:idx]:
                for p in tr0.iter(w("p")):
                    t = _norm(_text(p))
                    for key, label in META_LABELS.items():
                        if key not in meta and t.startswith(label):
                            meta[key] = p
                    if hosp is None and HOSPITAL_ACTION_LABEL in t:
                        hosp = p
            missing = [META_LABELS[k] for k in META_LABELS if k not in meta]
            if missing:
                raise TemplateError("The template is missing metadata labels: " + ", ".join(missing))

            margin = 108
            if styles_root is not None:
                for mar in styles_root.iter(w("tblCellMar")):
                    left = mar.find(w("left"))
                    if left is not None:
                        margin = int(left.get(w("w"), "108"))
                        break

            return TemplateMap(tbl, tr, idx, found, grid, standard, wide, meta, hosp, margin)

    raise TemplateError("Could not find the six-column observations table in the template.")


def _cell_for_field(tr, grid_col: int):
    for start, span, tc in _grid_layout(tr):
        if start <= grid_col < start + span:
            return tc
    return None


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------
def set_label_value(p, value: str):
    """Keep the label runs (up to and including the first ':'), drop any old
    value runs, append the new value in the label's formatting."""
    runs = p.findall(w("r"))
    label_end = None
    for i, r in enumerate(runs):
        if ":" in _text(r):
            label_end = i
            break
    if label_end is None:
        label_end = 0
    for r in runs[label_end + 1:]:
        p.remove(r)
    label_run = runs[label_end]
    # Trim anything typed after the colon inside the label run itself.
    ts = label_run.findall(w("t"))
    if ts:
        last = ts[-1]
        if last.text and ":" in last.text:
            head = last.text[: last.text.index(":") + 1]
            tail = last.text[last.text.index(":") + 1:]
            if tail.strip():
                last.text = head + " "
                last.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    value = " ".join(text_lines(value))  # single-line field
    if not value:
        return
    label_text = _text(p)
    prefix = "" if label_text.endswith(" ") else " "
    for r in make_runs(prefix + value, bold=True):
        p.append(r)


def add_hospital_action(p, value: str):
    lines = text_lines(value)
    if not lines:
        return
    ppr = p.find(w("pPr"))
    anchor = p
    for line in lines:
        np_ = _new_paragraph(ppr)
        for r in make_runs(line):
            np_.append(r)
        anchor.addnext(np_)
        anchor = np_


# --------------------------------------------------------------------------
# Pictures
# --------------------------------------------------------------------------
def _drawing_xml(rid: str, cx: int, cy: int, docpr_id: int, name: str) -> etree._Element:
    xml = f'''<w:drawing xmlns:w="{W_NS}" xmlns:wp="{WP_NS}" xmlns:a="{A_NS}" xmlns:pic="{PIC_NS}" xmlns:r="{R_NS}">
  <wp:inline distT="0" distB="0" distL="0" distR="0">
    <wp:extent cx="{cx}" cy="{cy}"/>
    <wp:effectExtent l="0" t="0" r="0" b="0"/>
    <wp:docPr id="{docpr_id}" name="{name}"/>
    <wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>
    <a:graphic>
      <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
        <pic:pic>
          <pic:nvPicPr><pic:cNvPr id="{docpr_id}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>
          <pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
          <pic:spPr>
            <a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
            <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
          </pic:spPr>
        </pic:pic>
      </a:graphicData>
    </a:graphic>
  </wp:inline>
</w:drawing>'''
    return etree.fromstring(xml)


def fit_size(w_px: int, h_px: int, max_w_emu: int, max_h_emu: int) -> Tuple[int, int]:
    scale = min(max_w_emu / w_px, max_h_emu / h_px)
    return max(1, int(w_px * scale)), max(1, int(h_px * scale))


# --------------------------------------------------------------------------
# Package helpers
# --------------------------------------------------------------------------
class _Package:
    def __init__(self, template_bytes: bytes):
        try:
            self.zin = zipfile.ZipFile(io.BytesIO(template_bytes))
            bad = self.zin.testzip()
        except zipfile.BadZipFile:
            raise TemplateError("The Word template is not a valid .docx file.")
        if bad:
            raise TemplateError("The Word template is corrupted.")
        names = self.zin.namelist()
        for required in ("[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels"):
            if required not in names:
                raise TemplateError("The Word template is incomplete (missing %s)." % required)
        self.names = names
        self.replaced: Dict[str, bytes] = {}
        self.added: Dict[str, bytes] = {}
        self.removed: set = set()

    def xml(self, name):
        try:
            return etree.fromstring(self.zin.read(name))
        except etree.XMLSyntaxError:
            raise TemplateError(f"The Word template part {name} is corrupted.")

    def write(self) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in self.names:
                if name in self.removed:
                    continue
                data = self.replaced.get(name)
                if data is None:
                    data = self.zin.read(name)
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(info, data)
            for name, data in self.added.items():
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                zout.writestr(info, data)
        return out.getvalue()


def _serialize(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def load_template_map(template_bytes: bytes) -> TemplateMap:
    """Validate a template without generating anything (used at startup)."""
    pkg = _Package(template_bytes)
    styles = pkg.xml("word/styles.xml") if "word/styles.xml" in pkg.names else None
    return analyze_template(pkg.xml("word/document.xml"), styles)


# --------------------------------------------------------------------------
# Row height estimate (for keep-row-on-one-page)
# --------------------------------------------------------------------------
def _estimate_text_height_in(value: str, width_twips: int) -> float:
    lines = text_lines(value)
    if not lines:
        return 0.0
    width_pt = max(20.0, (width_twips - 216) / 20.0)
    chars_per_line = max(8, int(width_pt / 5.6))       # ~11 pt Arabic text
    n = sum(max(1, -(-len(ln) // chars_per_line)) for ln in lines)
    return n * 0.24


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------
def build_report(template_bytes: bytes, report: ReportData) -> bytes:
    if not report.observations:
        raise ReportInputError("Add at least one observation before generating the report.")

    pkg = _Package(template_bytes)
    doc = pkg.xml("word/document.xml")
    styles = pkg.xml("word/styles.xml") if "word/styles.xml" in pkg.names else None
    rels = pkg.xml("word/_rels/document.xml.rels")
    ctypes = pkg.xml("[Content_Types].xml")

    tm = analyze_template(doc, styles)

    # ---------------- metadata ----------------
    set_label_value(tm.meta_paragraphs["facility"], report.facility)
    set_label_value(tm.meta_paragraphs["department"], report.department)
    set_label_value(tm.meta_paragraphs["date"],
                    arabic_date(report.round_date) if report.round_date else "")
    set_label_value(tm.meta_paragraphs["nurses"], report.nurses)
    set_label_value(tm.meta_paragraphs["patients"], report.patients)
    set_label_value(tm.meta_paragraphs["support_team"], report.support_team)
    if tm.hospital_action_paragraph is not None:
        add_hospital_action(tm.hospital_action_paragraph, report.hospital_action)

    # ---------------- observation rows ----------------
    std_proto = copy.deepcopy(tm.standard_proto)
    wide_proto = copy.deepcopy(tm.wide_proto) if tm.wide_proto is not None else None
    rows = tm.table.findall(w("tr"))
    for tr in rows[tm.header_index + 1:]:
        tm.table.remove(tr)

    # existing ids
    used_docpr = set()
    for name in pkg.names:
        if name.startswith("word/") and name.endswith(".xml"):
            for m in re.finditer(rb'<wp:docPr[^>]*\sid="(\d+)"', pkg.zin.read(name)):
                used_docpr.add(int(m.group(1)))
    next_docpr = [1000]

    def new_docpr():
        while next_docpr[0] in used_docpr:
            next_docpr[0] += 1
        used_docpr.add(next_docpr[0])
        return next_docpr[0]

    rel_ids = {r.get("Id") for r in rels}
    media_names = {n for n in pkg.names if n.startswith("word/media/")}
    photo_counter = [0]

    def add_image_part(photo: Photo) -> str:
        photo_counter[0] += 1
        n = photo_counter[0]
        part = f"word/media/crg_photo_{n}.{photo.ext}"
        while part in media_names:
            n += 1000
            part = f"word/media/crg_photo_{n}.{photo.ext}"
        media_names.add(part)
        pkg.added[part] = photo.data
        rid = f"rIdCrg{photo_counter[0]}"
        while rid in rel_ids:
            rid += "x"
        rel_ids.add(rid)
        rel = etree.SubElement(rels, "{%s}Relationship" % PKG_REL_NS)
        rel.set("Id", rid)
        rel.set("Type", IMAGE_REL_TYPE)
        rel.set("Target", part[len("word/"):])
        return rid

    ev_col = tm.field_cols["evidence"]
    ev_width_twips = tm.grid_widths[ev_col] if ev_col < len(tm.grid_widths) else 3435
    max_w_emu = int((ev_width_twips - 2 * tm.cell_margin_twips) * EMU_PER_TWIP
                    - PHOTO_SAFETY_MARGIN_IN * EMU_PER_INCH)

    for obs_no, obs in enumerate(report.observations, start=1):
        use_wide = bool(obs.wide and not obs.photos and wide_proto is not None)
        tr = copy.deepcopy(wide_proto if use_wide else std_proto)

        def cell(fld):
            return _cell_for_field(tr, tm.field_cols[fld])

        heights = []
        c = cell("observation")
        fill_cell_text(c, obs.text, color_text1=True)
        obs_width = ev_width_twips + tm.grid_widths[tm.field_cols["observation"]] if use_wide \
            else tm.grid_widths[tm.field_cols["observation"]]
        heights.append(_estimate_text_height_in(obs.text, obs_width))

        if not use_wide:
            ev = cell("evidence")
            ppr = _clear_cell(ev)
            if obs.photos:
                max_h_in = SINGLE_PHOTO_MAX_H_IN if len(obs.photos) == 1 else MULTI_PHOTO_MAX_H_IN
                max_h_emu = int(max_h_in * EMU_PER_INCH)
                total_h = 0.0
                for k, photo in enumerate(obs.photos, start=1):
                    rid = add_image_part(photo)
                    cx, cy = fit_size(photo.width_px, photo.height_px, max_w_emu, max_h_emu)
                    p = _new_paragraph(ppr)
                    r = etree.SubElement(p, w("r"))
                    r.append(_rpr("ar", no_proof=True, lang_ar=True))
                    did = new_docpr()
                    r.append(_drawing_xml(rid, cx, cy, did, f"Observation {obs_no} photo {k}"))
                    ev.append(p)
                    total_h += cy / EMU_PER_INCH + 0.2
                heights.append(total_h)
            else:
                p = _new_paragraph(ppr)
                for r in make_runs(NO_EVIDENCE_TEXT, no_proof=True, lang_ar=True):
                    p.append(r)
                ev.append(p)

        for fld, val, style in (
            ("recommendations", obs.recommendations, {}),
            ("repeated", REPEATED_AR.get(obs.repeated, ""), {"bold": True}),
            ("corrective_plan", obs.corrective_plan, {}),
            ("documentation", obs.documentation, {}),
        ):
            fill_cell_text(cell(fld), val, **style)
            heights.append(_estimate_text_height_in(val, tm.grid_widths[tm.field_cols[fld]]))

        # keep a row on one page when it reasonably fits on one page
        trpr = tr.find(w("trPr"))
        if trpr is None:
            trpr = etree.Element(w("trPr"))
            tr.insert(0, trpr)
        for old in trpr.findall(w("cantSplit")):
            trpr.remove(old)
        if max(heights or [0]) <= KEEP_TOGETHER_MAX_IN:
            trpr.insert(0, etree.Element(w("cantSplit")))

        tm.table.append(tr)

    # ---------------- trailing empty lines after the table ----------------
    _compact_trailing_paragraphs(tm.table)

    # ---------------- optional repeating header ----------------
    if report.repeat_header:
        _enable_repeating_header(tm)

    # ---------------- drop image relationships no longer referenced ------
    _prune_unused_images(pkg, doc, rels)

    _ensure_content_types(ctypes, {p.rsplit(".", 1)[1] for p in pkg.added})

    pkg.replaced["word/document.xml"] = _serialize(doc)
    pkg.replaced["word/_rels/document.xml.rels"] = _serialize(rels)
    pkg.replaced["[Content_Types].xml"] = _serialize(ctypes)
    return pkg.write()


def _compact_trailing_paragraphs(table):
    """The template ends with five empty 14 pt paragraphs after the table.
    When a generated table finishes near the bottom of a page they spill
    onto an extra, completely blank page. Word needs one paragraph after a
    table, so keep the first one (with its template formatting) but make it
    1 pt high, and drop the other empty ones. Non-empty paragraphs are never
    touched."""
    trailing = []
    nxt = table.getnext()
    while nxt is not None and nxt.tag == w("p"):
        trailing.append(nxt)
        nxt = nxt.getnext()
    if not trailing or any(_text(p).strip() or p.find(".//" + w("drawing")) is not None
                           for p in trailing):
        return
    keep = trailing[0]
    for p in trailing[1:]:
        p.getparent().remove(p)
    ppr = keep.find(w("pPr"))
    if ppr is None:
        ppr = etree.SubElement(keep, w("pPr"))
        keep.insert(0, ppr)
    for old in ppr.findall(w("spacing")):
        ppr.remove(old)
    sp = etree.Element(w("spacing"))
    sp.set(w("before"), "0")
    sp.set(w("after"), "0")
    sp.set(w("line"), "240")
    sp.set(w("lineRule"), "auto")
    # schema order inside pPr: ... bidi, spacing, ... jc ... rPr
    anchor = ppr.find(w("bidi"))
    if anchor is not None:
        anchor.addnext(sp)
    else:
        ppr.insert(0, sp)
    rpr = ppr.find(w("rPr"))
    if rpr is None:
        rpr = etree.SubElement(ppr, w("rPr"))
    for tag in ("sz", "szCs"):
        for old in rpr.findall(w(tag)):
            rpr.remove(old)
    insert_at = rpr.find(w("rtl"))
    for tag in ("sz", "szCs"):
        el = etree.Element(w(tag))
        el.set(w("val"), "2")
        if insert_at is not None:
            insert_at.addprevious(el)
        else:
            rpr.append(el)


def _enable_repeating_header(tm: TemplateMap):
    """Word only repeats header rows of tables that are not floating, and
    only a block of rows starting at the top of the table. The template's
    table is floating (positioned, centred) and starts with the blue
    metadata rows, so when the user asks for a repeating header we:
      * turn the floating table into an inline, centred, full-width table
        (same position on the page), and
      * mark every row from the top down to the column-header row as a
        header row (metadata + column titles repeat on each page)."""
    tblpr = tm.table.find(w("tblPr"))
    for tag in ("tblpPr", "tblOverlap"):
        el = tblpr.find(w(tag))
        if el is not None:
            tblpr.remove(el)
    if tblpr.find(w("jc")) is None:
        jc = etree.Element(w("jc"))
        jc.set(w("val"), "center")
        # schema order: ... bidiVisual, tblW, jc ...
        tblw = tblpr.find(w("tblW"))
        (tblw.addnext(jc) if tblw is not None else tblpr.append(jc))
    rows = tm.table.findall(w("tr"))
    for tr in rows[: tm.header_index + 1]:
        trpr = tr.find(w("trPr"))
        if trpr is None:
            trpr = etree.Element(w("trPr"))
            tr.insert(0, trpr)
        if trpr.find(w("tblHeader")) is None:
            trpr.append(etree.Element(w("tblHeader")))


def _prune_unused_images(pkg: _Package, doc, rels):
    used = set()
    for el in doc.iter():
        for k, v in el.attrib.items():
            if k.startswith("{%s}" % R_NS):
                used.add(v)
    for rel in list(rels):
        if rel.get("Type") == IMAGE_REL_TYPE and rel.get("Id") not in used:
            target = "word/" + rel.get("Target").lstrip("/").replace("word/", "", 1)
            rels.remove(rel)
            # only delete the media file if no other part refers to it
            still_used = False
            fname = target.rsplit("/", 1)[-1]
            for n in pkg.names:
                if n.endswith(".rels") and n != "word/_rels/document.xml.rels":
                    if fname.encode() in pkg.zin.read(n):
                        still_used = True
            if not still_used and target in pkg.names:
                pkg.removed.add(target)


def _ensure_content_types(ctypes, exts):
    have = {d.get("Extension").lower() for d in ctypes.findall("{%s}Default" % CT_NS)}
    mime = {"jpeg": "image/jpeg", "png": "image/png"}
    for ext in exts:
        if ext.lower() not in have:
            d = etree.Element("{%s}Default" % CT_NS)
            d.set("Extension", ext)
            d.set("ContentType", mime[ext])
            ctypes.insert(0, d)
