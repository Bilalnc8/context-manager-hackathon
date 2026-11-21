from emailctx.textnorm import evidence_in_source, normalize_match, normalize_requirement


def test_normalize_match_rules():
    s = "We need “CSV”​ exports — it’s\n  urgent"
    assert normalize_match(s) == 'We need "CSV" exports - it\'s urgent'


def test_evidence_matching_is_case_sensitive():
    assert evidence_in_source("need CSV", "We need CSV export")
    assert not evidence_in_source("need csv", "We need CSV export")


def test_evidence_matches_across_wrapping_and_typography():
    source = "we also need\r\n   “XLSX” export – soon"
    assert evidence_in_source('we also need "XLSX" export - soon', source)


def test_empty_evidence_never_matches():
    assert not evidence_in_source("  ​ ", "anything")


def test_normalize_requirement_ignores_case_and_punctuation():
    assert normalize_requirement("Export must support CSV.") == normalize_requirement(
        "export must  support csv"
    )
    assert normalize_requirement("v1.2") != normalize_requirement("v12")
