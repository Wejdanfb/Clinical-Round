"""Word-engine scenarios (template fidelity, photos, long text, many pages)."""
import io
import os
import sys
import zipfile
from datetime import date

import pytest
from lxml import etree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from report_builder import (Observation, ReportData, ReportInputError, TemplateError,  # noqa: E402
                            build_report, process_image)

FX = os.path.join(ROOT, "tests", "fixtures")
TEMPLATE = open(os.path.join(ROOT, "assets", "clinical_round_template.docx"), "rb").read()
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
LONG = ("لوحظ أثناء الجولة الإدارية عدم الالتزام الكامل بسياسة التعرف على المريض حيث أن أسورة التعريف "
        "لعدد من المرضى غير واضحة أو غير مقروءة بسبب تلف الطباعة. ") * 12


def photo(name):
    return process_image(open(os.path.join(FX, name), "rb").read(), name)


def build(obs, **meta):
    rd = ReportData(facility="مستشفى", department="ICU", round_date=date(2026, 10, 21), observations=obs, **meta)
    z = zipfile.ZipFile(io.BytesIO(build_report(TEMPLATE, rd)))
    doc = etree.fromstring(z.read("word/document.xml"))
    tbl = doc.find(W + "body").find(W + "tbl")
    return z, doc, tbl, tbl.findall(W + "tr")[3:]


def test_basic_and_template_integrity():
    z, doc, tbl, rows = build([Observation(text="ملاحظة\nNormal Saline", repeated="no", photos=[photo("portrait_1.jpg")])])
    t = zipfile.ZipFile(io.BytesIO(TEMPLATE))
    for n in t.namelist():
        if n not in ("word/document.xml", "word/_rels/document.xml.rels", "[Content_Types].xml"):
            assert z.read(n) == t.read(n), n
    tdoc = etree.fromstring(t.read("word/document.xml"))
    c14 = lambda e: etree.tostring(e, method="c14n")  # noqa: E731
    assert c14(doc.find(W + "body").find(W + "sectPr")) == c14(tdoc.find(W + "body").find(W + "sectPr"))
    assert c14(tbl.find(W + "tblGrid")) == c14(tdoc.find(W + "body").find(W + "tbl").find(W + "tblGrid"))
    assert len(rows) == 1 and len(rows[0].findall(W + "tc")[0].findall(W + "p")) == 2


def test_multiple_photos_fit_column_and_keep_ratio():
    _, _, _, rows = build([Observation(text="صور", repeated="no", photos=[
        photo("portrait_3.jpg"), photo("landscape_1.jpg"), photo("rotated_exif6.jpg"), photo("screenshot.png")])])
    ext = rows[0].findall(W + "tc")[1].findall(".//" + WP + "extent")
    assert len(ext) == 4
    assert all(int(e.get("cx")) < (3435 - 216) * 635 for e in ext)
    assert abs(int(ext[1].get("cx")) / int(ext[1].get("cy")) - 2000 / 1200) < 0.01
    assert int(ext[2].get("cy")) > int(ext[2].get("cx"))


def test_long_text_rows_may_break_short_rows_kept_together():
    _, _, _, rows = build([Observation(text=LONG, repeated="yes", recommendations=LONG),
                           Observation(text="قصير", repeated="no")])
    assert rows[0].find(W + "trPr/" + W + "cantSplit") is None
    assert rows[1].find(W + "trPr/" + W + "cantSplit") is not None


def test_many_observations_and_repeat_header_option():
    obs = [Observation(text=f"ملاحظة {i}", repeated="no", photos=[photo(f"portrait_{i % 8 + 1}.jpg")]) for i in range(14)]
    _, _, tbl, rows = build(obs)
    assert len(rows) == 14 and tbl.find(W + "tblPr/" + W + "tblpPr") is not None
    _, _, tbl2, _ = build(obs, repeat_header=True)
    assert len([r for r in tbl2.findall(W + "tr") if r.find(W + "trPr/" + W + "tblHeader") is not None]) == 3
    assert tbl2.find(W + "tblPr/" + W + "tblpPr") is None


def test_trailing_empty_paragraphs_compacted():
    _, doc, tbl, _ = build([Observation(text="x", repeated="no")])
    after = []
    n = tbl.getnext()
    while n is not None and n.tag == W + "p":
        after.append(n)
        n = n.getnext()
    assert len(after) == 1


def test_errors():
    with pytest.raises(ReportInputError):
        build([])
    with pytest.raises(TemplateError):
        build_report(b"not a docx", ReportData(observations=[Observation(text="x", repeated="no")]))
    with pytest.raises(ReportInputError):
        process_image(open(os.path.join(FX, "truncated.jpg"), "rb").read(), "t.jpg")
