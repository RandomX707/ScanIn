"""Declared-vs-rendered font disclosure and the configurable substitution table for page previews."""
import io

from reportlab.pdfgen import canvas

from packages.api.render import font_report
from packages.workers.office_render import DEFAULT_FONT_MAP, _font_substitution_xml, font_map


def pdf_with(*fonts: str) -> bytes:
    buf = io.BytesIO(); c = canvas.Canvas(buf)
    for i, f in enumerate(fonts):
        c.setFont(f, 12); c.drawString(72, 700 - 20 * i, "sample text"); 
    c.showPage(); c.save(); return buf.getvalue()


def test_metric_compatible_stand_ins_count_as_present_and_missing_fonts_are_disclosed():
    report = font_report(["Helvetica", "MingLiU_HKSCS-ExtB"], pdf_with("Helvetica"))
    assert [x["declared"] for x in report["substituted"]] == ["MingLiU_HKSCS-ExtB"]
    assert "Helvetica" in " ".join(report["rendered"])


def test_a_declared_font_whose_replacement_was_applied_reports_the_replacement():
    report = font_report(["MingLiU_HKSCS-ExtB"], pdf_with("Times-Roman"))
    assert report["substituted"][0]["replaced_with"] in ("Times New Roman", None)


def test_default_table_maps_windows_east_asian_latin_fonts_and_env_overrides(monkeypatch):
    assert font_map()["MingLiU_HKSCS-ExtB"] == "Times New Roman" and "SimSun" in DEFAULT_FONT_MAP
    monkeypatch.setenv("IDW_OFFICE_FONT_MAP", '{"CompanySans": "Arial"}')
    assert font_map()["CompanySans"] == "Arial"
    monkeypatch.setenv("IDW_OFFICE_FONT_MAP", "not json")
    assert "CompanySans" not in font_map()


def test_substitution_xml_is_well_formed_and_escapes_names():
    from lxml import etree
    xml = _font_substitution_xml({"A&B <Font>": "Times New Roman"})
    etree.fromstring(f'<root xmlns:oor="http://openoffice.org/2001/registry">{xml}</root>')
    assert "A&amp;B &lt;Font&gt;" in xml
