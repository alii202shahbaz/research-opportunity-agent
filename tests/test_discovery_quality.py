from datetime import date
from types import SimpleNamespace

from app.discovery import (
    _extract_country,
    _extract_deadline,
    _infer_domains,
    _infer_institution,
    _relevance,
    canonicalize_url,
)


def test_deadline_is_normalized_to_iso_date():
    parsed, note = _extract_deadline(
        "Image 9: Deadline: January 13, 2027. Apply Now."
    )
    assert parsed == date(2027, 1, 13)
    assert note == "Official deadline"


def test_deadline_supports_iso_and_ordinal_dates():
    assert _extract_deadline("Application deadline: 2027-02-04")[0] == date(2027, 2, 4)
    assert _extract_deadline("Apply by 13th January 2027")[0] == date(2027, 1, 13)


def test_domain_classification_is_not_limited_to_profile_interests():
    domains = _infer_domains(
        "This undergraduate research programme studies marine biodiversity and climate change.",
        ["Computer Vision"],
    )
    assert "Environmental Science" in domains
    assert "Computer Vision" not in domains


def test_multiple_domains_are_returned():
    domains = _infer_domains(
        "The project combines machine learning, medical imaging, and neuroscience."
    )
    assert "Artificial Intelligence" in domains
    assert "Computer Vision" in domains
    assert "Medicine" in domains


def test_relevance_does_not_drop_unmatched_disciplines():
    score = _relevance(
        "Undergraduate research experience programme with stipend and fellowship.",
        ["Computer Architecture"],
    )
    assert score >= 0.22


def test_social_media_sources_are_rejected():
    assert canonicalize_url("https://www.facebook.com/some/post") is None
    assert canonicalize_url("https://www.instagram.com/some/post") is None


def test_institution_comes_from_programme_not_social_host():
    assert _infer_institution(
        "Santa Fe Institute Undergraduate Research",
        "www.facebook.com",
        "Research programme at Santa Fe Institute.",
    ) == "Santa Fe Institute"


def test_country_is_a_short_value():
    assert _extract_country(
        "The programme is based in the United States and welcomes applicants."
    ) == "United States"


def test_unmatched_domains_are_not_mislabeled_interdisciplinary():
    assert _infer_domains("Undergraduate research opportunity with a funded fellowship.") == ["Unclassified"]


def test_ucr_is_normalized_to_full_institution_name():
    assert _infer_institution("UCR Summer Research", "www.ucr.edu", "UCR programme") == "University of California, Riverside"


def test_funding_parser_handles_no_tuition_or_travel_mentions():
    from app.discovery import _funding

    result = _funding("A summer undergraduate research programme offers a monthly stipend.")
    assert result[0] == "Partially funded"
    assert result[3] is None
    assert result[4] is None


def test_funding_parser_extracts_tuition_and_travel_before_summary():
    from app.discovery import _funding

    result = _funding("Fully funded programme with tuition waiver and travel grant.")
    assert result[0] == "Fully funded"
    assert result[3] is True
    assert result[4] is True
    assert "Tuition waiver" in result[1]
    assert "Travel grant" in result[1]


def test_funding_parser_does_not_claim_stipend_when_none_is_offered():
    from app.discovery import _funding

    result = _funding("This is an unpaid internship with no stipend.")
    assert result[0] == "Unpaid"
    assert "Stipend" not in result[1]
    assert "No stipend" in result[1]
