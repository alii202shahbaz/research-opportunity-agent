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


def test_information_security_is_a_distinct_domain():
    from app.discovery import _interest_domains

    assert _interest_domains(["Information Security"]) == {"Information Security"}
    assert "Computer Systems" not in _interest_domains(["Information Security"])


def test_queries_prioritize_requested_information_security():
    import json
    from types import SimpleNamespace
    from app.discovery import build_queries

    profile = SimpleNamespace(
        interests_json=json.dumps(["Information Security"]),
        preferred_countries_json=json.dumps(["Switzerland", "Germany"]),
        funding_preference="funded_only",
        intake_year=2027,
        intake_season="Summer",
        degree="Bachelor of Science",
        current_year="3rd year",
        current_semester=5,
        graduation_year=2028,
    )
    queries = build_queries(profile)
    assert queries
    valid_security_terms = [
        "information security", "cybersecurity", "network security", "application security",
        "cloud security", "malware analysis", "digital forensics", "cryptography",
        "penetration testing", "threat intelligence", "intrusion detection", "privacy engineering",
    ]
    assert all(any(term in q.lower() for term in valid_security_terms) for q in queries)
    assert not any("humanities social sciences arts" in q.lower() for q in queries)


def test_eligibility_breakdown_extracts_gpa_gender_and_degree():
    from types import SimpleNamespace
    from app.discovery import _eligibility_breakdown

    profile = SimpleNamespace(cgpa=3.5, cgpa_scale=4.0)
    details = _eligibility_breakdown(
        "Applicants must have a minimum GPA of 3.0/4.0. "
        "Undergraduate students only. Women applicants only. "
        "International applicants welcome.",
        profile,
    )
    assert details["minimum_cgpa"] == 3.0
    assert details["minimum_cgpa_scale"] == 4.0
    assert details["cgpa_match"] == "Meets requirement"
    assert details["gender_requirement"] == "Women only"
    assert details["degree_requirement"] == "Undergraduate only"
    assert details["citizenship_requirement"] == "International applicants welcome"


def test_us_citizenship_and_permanent_resident_only_rejects_pakistani_applicant():
    from types import SimpleNamespace
    from app.discovery import _eligibility

    profile = SimpleNamespace(
        degree="Bachelor of Science",
        current_year="Third year",
        current_semester=5,
        graduation_year=2028,
        citizenship="Pakistan",
        gender="Man",
        cgpa=3.5,
        cgpa_scale=4.0,
    )
    text = (
        "Applicants must be U.S. citizens or U.S. permanent residents. "
        "International students are ineligible."
    )
    assert _eligibility(text, profile) == "Ineligible"


def test_women_only_programme_rejects_man_and_is_unknown_without_gender():
    from types import SimpleNamespace
    from app.discovery import _eligibility

    base = dict(
        degree="Bachelor of Science", current_year="Third year",
        current_semester=5, graduation_year=2028, citizenship="Pakistan",
        cgpa=3.5, cgpa_scale=4.0,
    )
    assert _eligibility("Women applicants only.", SimpleNamespace(**base, gender="Man")) == "Ineligible"
    assert _eligibility("Women applicants only.", SimpleNamespace(**base, gender="")) == "Unknown"
    assert _eligibility("Women applicants only.", SimpleNamespace(**base, gender="Woman")) == "Possibly Eligible"


def test_directory_roundup_is_rejected_not_saved_as_a_programme():
    from app.discovery import _is_directory_page, _valid_program_title

    title = "Scholarships & Grants for Your Internship Abroad (2026) | Go Overseas"
    url = "https://www.gooverseas.com/blog/internship-abroad-scholarships-grants"
    assert _is_directory_page(title, url, "A scholarship roundup page with many listings.")
    assert not _valid_program_title(title)


def test_directory_links_expand_to_specific_programme_pages():
    from app.discovery import _directory_program_links

    markdown = (
        "# Undergraduate Cybersecurity Research\n"
        "[University Malware Research Fellowship](https://security.example.edu/reu/malware)\n"
        "[Apply Now](https://apply.example.edu/forms/123)\n"
        "[Student Resources](https://security.example.edu/resources)\n"
    )
    links = _directory_program_links(markdown, "https://directory.example.org/research", limit=10)
    assert len(links) == 1
    assert links[0]["title"] == "University Malware Research Fellowship"
    assert links[0]["url"] == "https://security.example.edu/reu/malware"


def test_markdown_application_link_is_specific_and_canonical():
    from app.discovery import _markdown_apply_url

    markdown = (
        "# Malware Research Fellowship\n"
        "[Programme details](https://security.example.edu/reu/malware)\n"
        "[Apply Now](https://apply.example.edu/forms/2027?utm_source=directory)\n"
    )
    assert _markdown_apply_url(markdown, "https://security.example.edu/reu/malware") == "https://apply.example.edu/forms/2027"


def test_information_security_child_page_produces_specific_candidate():
    import json
    from types import SimpleNamespace
    from app.discovery import extract_candidate

    profile = SimpleNamespace(
        intake_year=2027, intake_season="Summer",
        interests_json=json.dumps(["Information Security"]),
        preferred_countries_json=json.dumps(["Worldwide"]),
        funding_preference="funded_only",
        degree="Bachelor of Science", current_year="3rd year",
        current_semester=5, graduation_year=2028,
        citizenship="Pakistan", gender="Man", cgpa=3.5, cgpa_scale=4.0,
    )
    raw = (
        "# Summer 2027 Undergraduate Malware Analysis Research Fellowship\n"
        "This is a specific undergraduate research fellowship in information security and malware analysis. "
        "The program offers a fully funded summer research placement and a monthly stipend. "
        "Applications are open to international applicants. The application deadline is January 13, 2027. "
        "Undergraduate students may apply for the Summer 2027 cohort. "
        "[Apply Now](https://apply.rutgers.edu/research/malware-2027)\n"
    )
    result = extract_candidate({
        "url": "https://www.rutgers.edu/research/malware-fellowship-2027",
        "title": "Summer 2027 Undergraduate Malware Analysis Research Fellowship",
        "description": "Undergraduate malware analysis research fellowship.",
        "raw_content": raw,
        "score": 0.9,
    }, profile)
    assert result is not None
    assert result.institution == "Rutgers"
    assert "Information Security" in result.domains
    assert str(result.deadline) == "2027-01-13"
    assert result.eligibility == "Possibly Eligible"
    assert str(result.application_url) == "https://apply.rutgers.edu/research/malware-2027"


def test_cispa_is_recognized_as_an_official_research_institution():
    from app.discovery import _official_domain, _infer_institution

    assert _official_domain("https://career.cispa.de/internship")
    assert _infer_institution("CISPA Summer Research Internship Program", "career.cispa.de", "") == "CISPA Helmholtz Center for Information Security"


def test_specific_cispa_programme_is_not_treated_as_a_directory():
    from app.discovery import _is_directory_page

    assert not _is_directory_page(
        "CISPA Summer Research Internship Program",
        "https://career.cispa.de/internship",
        "Paid BSc/MSc research internship in computer security, privacy and AI.",
    )


def test_directory_search_is_expanded_and_only_child_programme_is_saved(monkeypatch):
    import json
    from types import SimpleNamespace
    import app.discovery as discovery

    directory_url = "https://www.ciee.org/go-abroad/research-opportunities"
    programme_url = "https://career.cispa.de/internship"
    directory_markdown = (
        "# Research opportunities and resources\\n"
        "[CISPA Summer Research (CSR) Internship Program 2027](https://career.cispa.de/internship)\\n"
        "[Apply Now](https://career.cispa.de/application)\\n"
    )
    programme_markdown = (
        "# CISPA Summer Research (CSR) Internship Program\\n"
        "The Summer 2027 programme offers BSc and MSc students a paid 2-3 month "
        "research internship in information security, computer security, cryptography, privacy "
        "and machine learning. Applicants must be enrolled in an eligible bachelor's or master's "
        "programme and have completed at least four semesters before the internship begins. "
        "International applicants are welcome. The application deadline is November 30, 2026. "
        "[Apply now](https://career.cispa.de/application/2027)\\n"
    )

    class FakeTavily:
        def __init__(self, api_key):
            self.calls = []

        def search(self, query, count=8):
            return [{
                "url": directory_url,
                "title": "Research opportunities and resources",
                "description": "Directory of research programmes.",
                "score": 0.95,
            }]

        def extract(self, urls):
            self.calls.append(list(urls))
            if directory_url in urls:
                return {directory_url: directory_markdown}
            if programme_url in urls:
                return {programme_url: programme_markdown}
            return {}

        def close(self):
            pass

    monkeypatch.setattr(discovery, "TavilySearch", FakeTavily)
    monkeypatch.setattr(discovery.settings, "tavily_api_key", "test-key")
    monkeypatch.setattr(discovery.settings, "max_search_queries_per_run", 3)
    monkeypatch.setattr(discovery.settings, "max_pages_per_run", 8)

    profile = SimpleNamespace(
        intake_year=2027,
        intake_season="Summer",
        interests_json=json.dumps(["Information Security"]),
        preferred_countries_json=json.dumps(["Germany"]),
        funding_preference="any",
        degree="Bachelor of Science",
        current_year="3rd year",
        current_semester=5,
        graduation_year=2028,
        citizenship="Pakistan",
        gender="Man",
        cgpa=3.5,
        cgpa_scale=4.0,
    )
    results = discovery.discover_for_profile(profile)
    assert len(results) == 1
    result = results[0]
    assert result.institution == "CISPA Helmholtz Center for Information Security"
    assert result.program_name == "CISPA Summer Research (CSR) Internship Program 2027"
    assert result.deadline == date(2026, 11, 30)
    assert "Information Security" in result.domains
    assert result.intake_confirmed is True
    assert str(result.application_url) == "https://career.cispa.de/application/2027"
    assert "directory" not in result.program_name.lower()


def test_stale_2026_page_is_not_promoted_as_2027_programme():
    import json
    from types import SimpleNamespace
    from app.discovery import extract_candidate

    profile = SimpleNamespace(
        intake_year=2027,
        intake_season="Summer",
        interests_json=json.dumps(["Information Security"]),
        preferred_countries_json=json.dumps(["Germany"]),
        funding_preference="any",
        degree="Bachelor of Science",
        current_year="3rd year",
        current_semester=5,
        graduation_year=2028,
        citizenship="Pakistan",
        gender="Man",
        cgpa=3.5,
        cgpa_scale=4.0,
    )
    old_page = (
        "# Summer 2026 Undergraduate Cybersecurity Research Fellowship\\n"
        + ("This was a 2026 research programme for undergraduate students. " * 12)
        + "[Apply now](https://apply.example.edu/2026)\\n"
    )
    assert extract_candidate({
        "url": "https://security.example.edu/research/fellowship-2026",
        "title": "Summer 2026 Undergraduate Cybersecurity Research Fellowship",
        "description": old_page,
        "raw_content": old_page,
        "score": 0.9,
    }, profile) is None


def test_notion_directory_cleanup_targets_roundups_not_specific_programmes():
    from app.notion import _is_legacy_directory_record

    assert _is_legacy_directory_record(
        "Scholarships & Grants for Your Internship Abroad (2026) | Go Overseas",
        "https://www.gooverseas.com/blog/internship-abroad-scholarships-grants",
    )
    assert _is_legacy_directory_record(
        "Summer Research Programs | Office of Undergraduate Research and Fellowships",
        "https://uraf.harvard.edu/summer-research-programs",
    )
    assert not _is_legacy_directory_record(
        "CISPA Summer Research Internship Program",
        "https://career.cispa.de/internship",
    )
