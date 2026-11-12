"""Rule-based policy understanding on synthetic outlines (no stakeholder data)."""
from packages.core import policy_sections as P


def L(t, y, s=9.0, b=False, x=40.0):
    return {"t": t, "x": x, "y": y, "s": s, "b": b, "n": 1}


OUTLINE = {"pages": [
    {"page": 1, "lines": [L("POLICY SCHEDULE", 40, 14, True), L("Policy No : P-123/45", 70), L("Period : 01-Jan-2026 to 31-Dec-2026", 85),
                         L("Insured Details", 120, 11, True), L("Name of the Insured : Example Traders Ltd", 140), L("Address : 1 Test Road, Sampletown", 155),
                         L("Sum Insured", 200, 11, True), L("Total Sum Insured : Rs. 50,00,000", 220),
                         L("Premium Details", 260, 11, True), L("Net Premium : 12,000", 280), L("GST @ 18% : 2,160", 295), L("Total Premium : 14,160", 310)]},
    {"page": 2, "lines": [L("GENERAL EXCLUSIONS", 40, 12, True), L("The company shall not be liable for war, nuclear risks or wilful acts.", 60),
                         L("WARRANTIES", 120, 12, True), L("It is warranted that fire extinguishers are maintained.", 140),
                         L("Terms and Conditions", 200, 12, True), L("This policy is subject to the conditions printed overleaf.", 220)]},
]}


def test_sections_are_logical_one_per_supported_category_with_anchors():
    r = P.segment(OUTLINE)
    keys = [g["key"] for g in r["sections"]]
    assert keys == ["schedule", "insured", "sum_insured", "premium", "exclusions", "warranties", "conditions"]
    assert "coverage" not in keys and "addons" not in keys                 # unsupported categories are never shown
    ex = next(g for g in r["sections"] if g["key"] == "exclusions")
    assert ex["pages"] == [2] and ex["occurrences"][0]["heading"] == "GENERAL EXCLUSIONS" and "war, nuclear" in ex["occurrences"][0]["excerpt"]


def test_facts_are_verbatim_and_page_anchored():
    chunks = {c["id"]: c for c in P.to_chunks(P.segment(OUTLINE))}
    facts = {(f["label"], f["value"]) for c in chunks.values() for f in c["facts"]}
    assert ("Policy number", "P-123/45") in facts and ("Sum insured", "Rs. 50,00,000") in facts
    assert ("Insured", "Example Traders Ltd") in facts and ("Premium", "14,160") in facts
    prem = chunks["sec-premium"]
    assert prem["source"]["pages"] == [1] and any(p.endswith("(p. 1)") for p in prem["points"])
    assert prem["summary"].startswith("Page 1.") and "GST" not in prem["summary"]


def test_nothing_is_invented_for_a_document_without_policy_headings():
    plain = {"pages": [{"page": 1, "lines": [L("Meeting notes", 40, 16, True), L("We talked about lunch.", 60)]}]}
    assert P.segment(plain)["sections"] == []
    fallback = P.outline_chunks(plain)
    assert fallback and fallback[0]["title"] == "Meeting notes" and "lunch" in fallback[0]["points"][0]


def test_page_label_groups_ranges():
    assert P.page_label([1, 3, 4, 5, 11]) == "pages 1, 3–5, 11" and P.page_label([7]) == "page 7"


def test_a_value_never_swallows_the_next_table_cell():
    line = {"t": "Insured : Example Traders Ltd Date : 01/01/2030", "x": 40.0, "y": 90.0, "s": 9.0, "b": False, "n": 2,
            "segs": [{"x": 40.0, "t": "Insured : Example Traders Ltd"}, {"x": 300.0, "t": "Date : 01/01/2030"}]}
    got = P.segment({"pages": [{"page": 1, "lines": [L("Insured details", 60, 12, True), line]}]})
    facts = [f for g in got["sections"] for f in g["facts"]]
    assert [(f["label"], f["value"]) for f in facts] == [("Insured", "Example Traders Ltd")]


def test_label_cell_with_colon_takes_the_next_cell_as_its_value():
    line = {"t": "Sum Insured : Rs. 5,00,000", "x": 40.0, "y": 90.0, "s": 9.0, "b": False, "n": 2,
            "segs": [{"x": 40.0, "t": "Sum Insured :"}, {"x": 200.0, "t": "Rs. 5,00,000"}]}
    got = P.segment({"pages": [{"page": 2, "lines": [L("Sums Insured", 60, 12, True), line]}]})
    assert [(f["label"], f["value"], f["page"]) for g in got["sections"] for f in g["facts"]] == [("Sum insured", "Rs. 5,00,000", 2)]


def test_insurer_address_is_not_filed_as_the_insureds():
    out = {"pages": [{"page": 1, "lines": [L("Insured details", 40, 12, True), L("Address : 1 Insurer Road, Branch Town", 60),
                                           L("Insured's Address : 9 Example Street", 80)]}]}
    facts = [(f["label"], f["value"]) for g in P.segment(out)["sections"] for f in g["facts"]]
    assert facts == [("Address", "9 Example Street")]            # the bare label (the insurer's office) is not promoted


def test_unstyled_documents_still_surface_premium_warranty_and_exclusion_statements():
    flat = {"pages": [{"page": 3, "lines": [L("Name of the Proposer Example Traders Total Premium 1,200.00", 40), L("IGST(18%) 216.00", 55),
                                            L("Policy is subject to the following Warranty: see schedule", 70), L("Exclusion No 4 A of the Section What We Exclude", 85),
                                            L("Plain sentence that mentions premium in passing and goes on for a while.", 100)]}]}
    got = {g["key"]: g for g in P.segment(flat)["sections"]}
    assert {"premium", "warranties", "exclusions"} <= set(got)
    assert got["premium"]["pages"] == [3] and got["warranties"]["occurrences"][0]["excerpt"].startswith("Policy is subject")
    assert not any("passing" in o["excerpt"] for g in got.values() for o in g["occurrences"])    # no loose mentions


def test_a_neighbouring_label_cell_is_never_taken_as_the_value():
    line = {"t": "Period of Insurance : Policy Period :", "x": 40.0, "y": 90.0, "s": 9.0, "b": False, "n": 2,
            "segs": [{"x": 40.0, "t": "Period of Insurance :"}, {"x": 200.0, "t": "Policy Period :"}]}
    got = P.segment({"pages": [{"page": 1, "lines": [L("Policy schedule", 60, 12, True), line]}]})
    assert [f for g in got["sections"] for f in g["facts"]] == []
