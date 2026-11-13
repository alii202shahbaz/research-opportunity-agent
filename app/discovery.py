from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import UserOpportunity, UserProfile

logger = logging.getLogger(__name__)
settings = get_settings()


class DiscoveredOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    program_name: str = Field(min_length=3, max_length=400)
    institution: str = Field(default="Not identified", max_length=240)
    country: str = Field(default="Not specified", max_length=160)
    domains: list[str] = Field(default_factory=list)
    funding_type: str = "Not disclosed"
    funding_details: str = ""
    stipend: str = "Not specified"
    tuition_waiver: bool | None = None
    travel_grant: bool | None = None
    housing: str = "Not specified"
    deadline: date | None = None
    deadline_note: str = ""
    application_url: HttpUrl
    official_source_url: HttpUrl
    verification_status: str = "Needs Review"
    intake_year: int = 2027
    intake_confirmed: bool = False
    eligibility: str = "Unknown"
    opportunity_status: str = "Unknown"
    relevance_score: float = Field(default=0.0, ge=0, le=1)


class TavilySearch:
    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.Client(timeout=httpx.Timeout(35.0, connect=10.0), follow_redirects=False)

    def search(self, query: str, count: int = 8) -> list[dict]:
        response = None
        for attempt in range(4):
            response = self.client.post(
                self.ENDPOINT,
                json={
                    "query": query,
                    "max_results": max(1, min(20, count)),
                    "search_depth": "basic",
                    "topic": "general",
                    "include_answer": False,
                    "include_raw_content": False,
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            if response.status_code not in {429, 500, 502, 503, 504, 529} or attempt == 3:
                break
            time.sleep(min(8.0, 0.75 * (2 ** attempt)))
        response.raise_for_status()
        results = response.json().get("results", [])
        # The discovery pipeline consumes a search-engine neutral shape.
        return [
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "description": item.get("content", ""),
            }
            for item in results
            if item.get("url")
        ]

    def close(self) -> None:
        self.client.close()


def canonicalize_url(raw_url: str) -> str | None:
    try:
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname in {"localhost", "localhost.localdomain"}:
            return None
        blocked_hosts = {
            "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
            "tiktok.com", "youtube.com", "youtu.be", "threads.net", "reddit.com",
            "scholarshipportal.com", "scholarships.com", "scholarshiproar.com",
            "scholarshippositions.com", "opportunitiescorners.com",
            "opportunitiesforafricans.com", "fullyfundedscholarships.com",
        }
        if any(hostname == blocked or hostname.endswith("." + blocked) for blocked in blocked_hosts):
            return None
        try:
            ip = ipaddress.ip_address(hostname)
            if not ip.is_global:
                return None
        except ValueError:
            pass
        if parsed.port and parsed.port not in {80, 443}:
            return None
        kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        return urlunparse((parsed.scheme.lower(), hostname, path.rstrip("/") or "/", "", urlencode(kept), ""))
    except Exception:
        return None


def _safe_public_host(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        direct = ipaddress.ip_address(host)
        return direct.is_global
    except ValueError:
        pass
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return False
    for entry in addresses:
        try:
            if not ipaddress.ip_address(entry[4][0]).is_global:
                return False
        except ValueError:
            return False
    return bool(addresses)


def fetch_page(url: str) -> tuple[str, str, str | None] | None:
    current = url
    headers = {"User-Agent": "ResearchOpportunityAgent/0.1 (research programme metadata collector)",
               "Accept": "text/html,application/xhtml+xml"}
    try:
        with httpx.Client(timeout=httpx.Timeout(18.0, connect=7.0), follow_redirects=False, trust_env=False) as client:
            for _ in range(4):
                if not _safe_public_host(current):
                    return None
                with client.stream("GET", current, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        return None
                    content_type = response.headers.get("content-type", "").lower()
                    if "html" not in content_type:
                        return None
                    declared_length = response.headers.get("content-length")
                    if declared_length and declared_length.isdigit() and int(declared_length) > 1_500_000:
                        return None
                    chunks = []
                    total_bytes = 0
                    for chunk in response.iter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > 1_500_000:
                            return None
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    html = b"".join(chunks).decode(encoding, errors="replace")
                soup = BeautifulSoup(html, "html.parser")
                for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
                    node.decompose()
                title = soup.title.get_text(" ", strip=True) if soup.title else ""
                description = ""
                meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
                if meta:
                    description = meta.get("content", "")
                main = soup.find("main") or soup.find("article") or soup.body or soup
                text = " ".join(main.stripped_strings)
                apply_url = None
                apply_terms = re.compile(r"^(?:apply now|apply here|online application|application portal|submit application|apply for (?:the )?(?:programme|program|internship|fellowship))$", re.I)
                for anchor in main.find_all("a", href=True):
                    anchor_text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
                    href = urljoin(current, anchor.get("href", ""))
                    canonical_href = canonicalize_url(href)
                    if anchor_text and apply_terms.search(anchor_text) and canonical_href:
                        apply_url = canonical_href
                        break
                if description:
                    text = description + "\n" + text
                return title[:400], text[:35_000], apply_url
    except (httpx.HTTPError, ValueError, UnicodeError):
        return None
    return None


def _academic_level(degree: str) -> str:
    low = (degree or "").lower()
    if any(term in low for term in ["phd", "ph.d", "doctorate", "doctoral"]):
        return "doctoral PhD"
    if any(term in low for term in ["master", "msc", "m.sc", "ms degree", "graduate"]):
        return "masters graduate"
    if any(term in low for term in ["bachelor", "bsc", "b.sc", "bs degree", "undergraduate"]):
        return "undergraduate bachelor"
    return degree.strip() or "student"


def build_queries(profile: UserProfile) -> list[str]:
    interests = json.loads(profile.interests_json or "[]")
    countries = json.loads(profile.preferred_countries_json or "[]")
    location = " ".join(countries[:3])
    funded = "funded stipend scholarship paid travel grant accommodation"
    year = str(profile.intake_year)
    season = profile.intake_season.lower() if profile.intake_season != "Any" else "(summer OR winter OR spring OR autumn)"
    level = _academic_level(getattr(profile, "degree", ""))
    graduation_year = getattr(profile, "graduation_year", None)
    grad_context = f"class of {graduation_year}" if graduation_year else ""
    year_context = getattr(profile, "current_year", "") or ""
    semester = getattr(profile, "current_semester", None)
    if semester:
        suffix = "th" if 10 <= semester % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(semester % 10, "th")
        semester_context = f"{semester}{suffix} semester"
    else:
        semester_context = ""

    # Discovery is intentionally not restricted to the user's interests.
    # Broad programme searches cover multiple disciplines; interests are used
    # later as a relevance signal, never as an exclusion rule.
    queries = [
        f'{year} {season} undergraduate research programmes internships fellowships international students {funded} {location}',
        f'{year} {season} undergraduate summer research opportunities science humanities social sciences arts engineering {location}',
        f'{year} {season} research experience undergraduate students international applicants stipend application deadline {location}',
    ]
    grouped_interests = [interests[i:i + 2] for i in range(0, min(len(interests), 15), 2)]
    for group in grouped_interests:
        interest_clause = " OR ".join(f'"{interest}"' for interest in group)
        queries.append(
            f'({interest_clause}) {season} {year} {level} {year_context} {semester_context} '
            f'research internship fellowship scholarship {funded} {location}'
        )
    queries.append(
        f'{year} {season} international undergraduate research fellowship programme '
        f'cross-disciplinary global university institute application {grad_context} {location}'
    )
    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))[: settings.max_search_queries_per_run]


def _official_domain(url: str) -> bool:
    host = urlparse(url).hostname or ""
    parts = host.lower().split(".")
    return host.endswith((".edu", ".gov", ".ac.uk", ".edu.au", ".ac.nz", ".edu.cn")) or any(
        part in {"edu", "ac", "gov"} for part in parts[:-1]
    )


def _extract_country(text: str) -> str:
    countries = [
        "United States", "United Kingdom", "United Arab Emirates", "Saudi Arabia",
        "South Korea", "North Korea", "New Zealand", "South Africa", "Saudi Arabia",
        "Pakistan", "Canada", "Australia", "Germany", "France", "Switzerland",
        "Netherlands", "Sweden", "Norway", "Denmark", "Finland", "Belgium",
        "Austria", "Italy", "Spain", "Portugal", "Ireland", "Poland", "Czech Republic",
        "Japan", "China", "India", "Singapore", "Malaysia", "Indonesia", "Thailand",
        "Vietnam", "Philippines", "Brazil", "Mexico", "Argentina", "Chile", "Egypt",
        "Turkey", "Türkiye", "Israel", "Jordan", "Qatar", "Kuwait", "Oman",
        "Kenya", "Nigeria", "Ghana", "Morocco", "Tunisia", "Bangladesh", "Sri Lanka",
        "Nepal", "Taiwan", "Hong Kong", "Ukraine", "Greece", "Romania", "Hungary",
    ]
    low = text.lower()
    for country in sorted(set(countries), key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(country.lower()) + r"(?![a-z])", low):
            return country
    match = re.search(r"(?:location|country|based in)\s*:\s*([^\n.;]{2,60})", text, re.I)
    if match:
        candidate = re.split(r"[,|]", match.group(1))[0].strip()
        if 2 <= len(candidate) <= 40 and len(candidate.split()) <= 4:
            return candidate
    return "Not specified"


def _infer_institution(title: str, host: str, text: str) -> str:
    haystack = f"{title} {text[:9000]}"
    low = haystack.lower()
    known = [
        "Santa Fe Institute", "University of California, Riverside", "UC Riverside",
        "University of California Riverside", "UCR", "University of California",
        "EPFL", "ETH Zurich", "MIT", "Stanford University", "Carnegie Mellon University",
        "University of Oxford", "University of Cambridge", "Max Planck Institute",
        "CERN", "RIKEN", "KAUST", "National University of Singapore", "Nanyang Technological University",
        "Harvard University", "Princeton University", "Yale University", "Cornell University",
        "University of Toronto", "University of British Columbia", "University of Melbourne",
    ]
    for name in known:
        if name.lower() in low:
            if name.lower() in {"ucr", "uc riverside", "university of california riverside"}:
                return "University of California, Riverside"
            return name
    patterns = [
        r"\b((?:[A-Z][A-Za-z&.'-]+\s+){0,5}University\s+of\s+[A-Z][A-Za-z&.' -]{2,60})\b",
        r"\b((?:[A-Z][A-Za-z&.'-]+\s+){0,5}[A-Z][A-Za-z&.'-]+\s+University)\b",
        r"\b((?:[A-Z][A-Za-z&.'-]+\s+){0,5}[A-Z][A-Za-z&.'-]+\s+(?:Institute|College|Laboratory))\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, haystack)
        if match:
            candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,-")
            if len(candidate) <= 90 and not candidate.lower().startswith(("the application", "the research")):
                return candidate
    host = (host or "").lower().split(":")[0].removeprefix("www.")
    if _official_domain("https://" + host):
        label = host.split(".")[0]
        if label not in {"facebook", "instagram", "linkedin", "twitter", "tiktok", "youtube"}:
            return label.replace("-", " ").title()
    return "Not identified"


DOMAIN_RULES = {
    "Computer Science": ["computer science", "software engineering", "programming languages", "algorithms", "data structures", "operating systems", "distributed systems", "database systems", "cloud computing", "web development"],
    "Artificial Intelligence": ["artificial intelligence", "machine learning", "deep learning", "neural network", "generative ai", "large language model", "natural language processing", "reinforcement learning", "data mining"],
    "Computer Vision": ["computer vision", "image processing", "visual recognition", "object detection", "image segmentation", "medical imaging", "3d vision", "video analytics"],
    "Computer Systems": ["computer architecture", "processor design", "risc-v", "riscv", "gpu architecture", "hardware accelerator", "microarchitecture", "embedded systems", "computer networks", "cybersecurity", "information security", "cryptography"],
    "Robotics": ["robotics", "autonomous systems", "robot manipulation", "motion planning", "human robot interaction"],
    "Mathematics": ["mathematics", "pure mathematics", "applied mathematics", "number theory", "topology", "algebra", "calculus"],
    "Statistics": ["statistics", "statistical learning", "probability", "biostatistics", "econometrics", "bayesian"],
    "Physics": ["physics", "quantum", "astrophysics", "particle physics", "condensed matter", "optics", "photonics", "nuclear physics"],
    "Chemistry": ["chemistry", "chemical synthesis", "organic chemistry", "inorganic chemistry", "catalysis", "analytical chemistry"],
    "Biology": ["biology", "molecular biology", "cell biology", "genetics", "genomics", "microbiology", "biotechnology", "bioinformatics"],
    "Medicine": ["medicine", "clinical research", "clinical trial", "medical research", "oncology", "neuroscience", "public health", "epidemiology"],
    "Psychology": ["psychology", "cognitive science", "behavioral science", "mental health", "neuropsychology"],
    "Environmental Science": ["environmental science", "climate change", "ecology", "biodiversity", "conservation", "sustainability", "earth science"],
    "Engineering": ["engineering", "mechanical engineering", "electrical engineering", "civil engineering", "chemical engineering", "aerospace", "industrial engineering"],
    "Materials Science": ["materials science", "nanotechnology", "nanomaterials", "polymers", "semiconductor materials", "metallurgy"],
    "Energy": ["renewable energy", "solar energy", "wind energy", "battery", "energy storage", "power systems", "hydrogen"],
    "Agriculture": ["agriculture", "agronomy", "crop science", "food science", "plant science", "animal science", "forestry"],
    "Economics": ["economics", "microeconomics", "macroeconomics", "development economics", "labor economics"],
    "Finance": ["finance", "financial markets", "banking", "investment", "risk management", "accounting"],
    "Business": ["business", "management", "entrepreneurship", "marketing", "supply chain", "operations management"],
    "Law": ["law", "legal studies", "constitutional law", "human rights law", "international law", "criminology"],
    "Political Science": ["political science", "public policy", "international relations", "governance", "political economy", "diplomacy"],
    "Sociology": ["sociology", "social science", "social policy", "demography", "social inequality", "community research"],
    "Anthropology": ["anthropology", "archaeology", "ethnography", "cultural heritage"],
    "Education": ["education", "pedagogy", "educational technology", "learning sciences", "curriculum"],
    "Linguistics": ["linguistics", "language acquisition", "phonetics", "computational linguistics", "translation studies"],
    "Literature & History": ["literature", "history", "historical research", "classics", "cultural studies", "religious studies"],
    "Philosophy & Ethics": ["philosophy", "ethics", "bioethics", "moral philosophy", "technology ethics"],
    "Arts & Design": ["arts", "fine arts", "graphic design", "interaction design", "visual arts", "music research", "film studies"],
    "Architecture & Urban Studies": ["architecture", "urban planning", "urban studies", "built environment", "landscape architecture"],
    "Communication & Media": ["communication", "media studies", "journalism", "digital media", "film and media", "media forensics"],
    "Data Science": ["data science", "data analytics", "big data", "data engineering", "data visualization"],
    "Astronomy & Space Science": ["astronomy", "astrophysics", "planetary science", "space science", "aerospace science", "space exploration"],
    "Geosciences": ["geoscience", "geology", "geophysics", "oceanography", "meteorology", "seismology", "hydrology"],
    "Public Administration": ["public administration", "public management", "public affairs", "policy analysis"],
    "Sports Science": ["sports science", "sport science", "kinesiology", "exercise science", "sport psychology"],
    "Food Science": ["food science", "food technology", "nutrition science", "food safety"],
    "Tourism & Hospitality": ["tourism", "hospitality", "hotel management", "event management"],
    "Interdisciplinary Research": ["interdisciplinary", "multidisciplinary", "cross-disciplinary"],
}


def _infer_domains(text: str, interests: list[str] | None = None) -> list[str]:
    low = re.sub(r"\s+", " ", (text or "").lower())
    matches = []
    for domain, keywords in DOMAIN_RULES.items():
        if any(re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", low) for keyword in keywords):
            matches.append(domain)
    if matches:
        return matches[:5]
    # A programme can be interdisciplinary even when the page uses unusual
    # terminology. Never force a user's unrelated interests into its subject.
    return ["Unclassified"]


def _extract_deadline(text: str) -> tuple[date | None, str]:
    if not text:
        return None, "Not published"
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    date_patterns = [
        r"\b20\d{2}-\d{1,2}-\d{1,2}\b",
        rf"\b{month}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+20\d{{2}}\b",
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{month}\.?[,]?\s+20\d{{2}}\b",
        r"\b\d{1,2}/\d{1,2}/20\d{2}\b",
    ]
    marker = re.compile(
        r"(?:application\s+)?(?:deadline|closing\s+date|apply\s+by|applications?\s+(?:close|due)|due\s+date|last\s+date\s+to\s+apply|submit\s+by)",
        re.I,
    )
    formats = ["%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
               "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%d/%m/%Y"]
    for hit in marker.finditer(text):
        window = text[max(0, hit.start() - 65):min(len(text), hit.end() + 190)]
        normalized = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", window, flags=re.I)
        candidates = []
        for pattern in date_patterns:
            for match in re.finditer(pattern, normalized, re.I):
                raw = match.group(0).replace(".", "")
                raw = re.sub(r"^Sept\\b", "Sep", raw, flags=re.I)
                parsed = None
                for fmt in formats:
                    try:
                        parsed = datetime.strptime(raw, fmt).date()
                        break
                    except ValueError:
                        continue
                if parsed and parsed.year >= datetime.now().year - 1:
                    candidates.append((abs(match.start() - (hit.start() - max(0, hit.start()-65) + hit.end() - hit.start())), parsed))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1], "Official deadline"
    return None, "Not published"


def _funding(text: str) -> tuple[str, str, str, bool | None, bool | None, str]:
    low = text.lower()
    full = any(x in low for x in ["fully funded", "all expenses covered", "all costs covered", "full funding", "full scholarship"])
    unpaid = any(x in low for x in ["unpaid internship", "no stipend", "without stipend", "not funded", "unfunded"])
    stipend_matches = re.findall(r"(?:USD|US\$|\$|€|EUR|CHF|GBP|£|CAD|AUD|INR|CNY)\s?[\d,]+(?:\.\d+)?(?:\s?(?:per|\/)?\s?(?:month|week|hour|year|day))?", text, re.I)
    stipend = stipend_matches[0][:120] if stipend_matches else "Not specified"
    stipend_flag = any(x in low for x in ["stipend", "monthly allowance", "paid internship", "salary", "remuneration"])
    scholarship = any(x in low for x in ["scholarship", "financial support", "grant", "tuition waiver"])
    if full:
        funding_type = "Fully funded"
    elif unpaid:
        funding_type = "Unpaid"
    elif stipend_flag or scholarship:
        funding_type = "Partially funded"
    else:
        funding_type = "Not disclosed"
    funding_phrases = []
    if full:
        funding_phrases.append("Full funding")
    if stipend_flag:
        funding_phrases.append("Stipend")
    if tuition is True:
        funding_phrases.append("Tuition waiver")
    if travel is True:
        funding_phrases.append("Travel grant")
    if any(x in low for x in ["free accommodation", "housing provided", "accommodation provided", "housing covered"]):
        funding_phrases.append("Housing")
    details = " · ".join(funding_phrases[:5]) or "Not specified"
    tuition = True if "tuition waiver" in low or "tuition covered" in low else (False if "tuition is not covered" in low else None)
    travel = True if "travel grant" in low or "travel allowance" in low or "travel costs covered" in low else (False if "travel costs are not covered" in low else None)
    housing = "Provided / covered" if any(x in low for x in ["free accommodation", "housing provided", "accommodation provided", "housing covered"]) else "Not specified"
    return funding_type, details, stipend, tuition, travel, housing


def _relevance(text: str, interests: list[str]) -> float:
    low = (text or "").lower()
    matches = sum(1 for interest in interests if interest.lower() in low)
    programme_signals = [
        "undergraduate research", "research experience", "research internship",
        "summer research", "research fellowship", "student researcher",
        "undergraduate students", "research programme", "research program",
    ]
    programme_hits = sum(1 for term in programme_signals if term in low)
    funding_hits = sum(1 for term in ["stipend", "fully funded", "scholarship", "travel grant", "housing"] if term in low)
    # Every genuine research programme gets a base score. User interests
    # improve ranking but cannot suppress other disciplines.
    score = 0.22 + min(0.36, programme_hits * 0.06) + min(0.18, funding_hits * 0.03)
    if interests:
        score += min(0.24, 0.24 * matches / len(interests))
    return round(min(1.0, score), 3)


def _ordinal_number(value: str) -> int | None:
    words = {"first": 1, "freshman": 1, "second": 2, "sophomore": 2,
             "third": 3, "junior": 3, "fourth": 4, "senior": 4,
             "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10}
    normalized = value.lower().strip()
    if normalized in words:
        return words[normalized]
    match = re.search(r"\d+", normalized)
    return int(match.group(0)) if match else None


def _profile_academic_semester(value: str) -> int | None:
    low = (value or "").lower()
    ordinals = r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,2}(?:st|nd|rd|th)?)"
    match = re.search(rf"\b{ordinals}\s*(?:academic\s*)?(?:semester|sem)\b", low)
    if not match:
        match = re.search(r"\b(?:semester|sem)\s*(\d{1,2})\b", low)
    return _ordinal_number(match.group(1)) if match else None


def _profile_academic_year(value: str) -> int | None:
    low = (value or "").lower()
    for word, number in [("freshman", 1), ("sophomore", 2), ("junior", 3), ("senior", 4)]:
        if word in low:
            return number
    match = re.search(r"(\d+)(?:st|nd|rd|th)?\s*(?:academic\s*)?year\b", low)
    return int(match.group(1)) if match else None


def _gpa_minimum(text: str) -> tuple[float, float] | None:
    patterns = [
        r"(?:minimum|min\.?|at least|no less than|required)\s+(?:a\s+)?(?:cumulative\s+)?(?:gpa|cgpa|grade point average)\s*(?:of|:)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:/\s*(\d{1,3}(?:\.\d+)?))?",
        r"(?:gpa|cgpa|grade point average).{0,35}?(?:minimum|min\.?|at least|no less than).{0,20}?(\d{1,3}(?:\.\d+)?)\s*(?:/\s*(\d{1,3}(?:\.\d+)?))?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if not match:
            continue
        score = float(match.group(1))
        scale = float(match.group(2)) if match.group(2) else None
        if scale is None:
            context = text[max(0, match.start()-80):match.end()+100]
            scale_match = re.search(r"(?:on|out of)\s+(?:a\s+)?(4(?:\.0)?|5(?:\.0)?|10(?:\.0)?|100)(?:-point)?\s+scale", context, re.I)
            if scale_match:
                scale = float(scale_match.group(1))
        if scale is not None and 0 < score <= scale <= 100:
            return score, scale
    return None


def _eligibility(text: str, profile: UserProfile) -> str:
    low = text.lower()
    degree = (getattr(profile, "degree", "") or "").lower()
    level = _academic_level(degree)
    reasons_known = False

    if any(term in low for term in ["undergraduate students only", "undergraduates only", "bachelor's students only", "bachelor students only"]):
        reasons_known = True
        if level.startswith(("masters", "doctoral")):
            return "Ineligible"
    if any(term in low for term in ["graduate students only", "master's students only", "masters students only"]):
        reasons_known = True
        if level.startswith(("undergraduate", "doctoral")):
            return "Ineligible"
    if any(term in low for term in ["phd students only", "doctoral students only", "must be a phd student"]):
        reasons_known = True
        if not level.startswith("doctoral"):
            return "Ineligible"

    profile_year_text = getattr(profile, "current_year", "") or ""
    current_semester = getattr(profile, "current_semester", None)
    if current_semester is None:
        current_semester = _profile_academic_semester(profile_year_text)
    current_year = _profile_academic_year(profile_year_text)

    min_semester_match = re.search(
        r"(?:at least|minimum of|must be in|currently in at least|from)\s+(?:the\s+)?"
        r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,2}(?:st|nd|rd|th)?)"
        r"\s*(?:academic\s*)?(?:semester|sem)\b",
        low,
    )
    if min_semester_match:
        required_semester = _ordinal_number(min_semester_match.group(1))
        if current_semester is not None and required_semester is not None:
            if current_semester < required_semester:
                return "Ineligible"
            reasons_known = True

    min_year_match = re.search(
        r"(?:at least|minimum of|must be in|currently in at least)\s+(?:the\s+)?(first|second|third|fourth|fifth|sixth|\d+(?:st|nd|rd|th)?)\s*(?:academic\s*)?year",
        low,
    )
    if min_year_match and current_year is not None:
        required_year = _ordinal_number(min_year_match.group(1))
        if required_year and current_year < required_year:
            return "Ineligible"
        reasons_known = True

    graduation_match = re.search(
        r"(?:expected to graduate|graduating|graduation date).{0,35}?(?:in|by|after|before)?\s*(20\d{2})",
        low,
    )
    graduation_year = getattr(profile, "graduation_year", None)
    if graduation_match and graduation_year:
        required_graduation = int(graduation_match.group(1))
        graduation_context = low[max(0, graduation_match.start()-30):graduation_match.end()]
        if "after" in graduation_context and graduation_year <= required_graduation:
            return "Ineligible"
        if "before" in graduation_context and graduation_year >= required_graduation:
            return "Ineligible"
        reasons_known = True

    if any(term in low for term in ["u.s. citizens only", "us citizens only", "must be a u.s. citizen", "must be a us citizen"]):
        reasons_known = True
        citizenship = (getattr(profile, "citizenship", "") or "").lower()
        if citizenship and not any(term in citizenship for term in ["united states", "usa", "american", "u.s."]):
            return "Ineligible"

    gpa_requirement = _gpa_minimum(text)
    if gpa_requirement:
        required_gpa, required_scale = gpa_requirement
        cgpa = getattr(profile, "cgpa", None)
        cgpa_scale = getattr(profile, "cgpa_scale", 4.0)
        if cgpa is not None and float(cgpa_scale or 4.0) == required_scale:
            reasons_known = True
            if cgpa < required_gpa:
                return "Ineligible"
        # Different grading systems are not converted using an unreliable ratio.

    # Explicit nationality restrictions are checked against the user's
    # citizenship; do not infer eligibility from a programme's country.
    citizenship = (getattr(profile, "citizenship", "") or "").lower()
    nationality_rules = [
        (r"(?:only\s+)?u\.?s\.?\s+citizens(?:\s+or\s+permanent\s+residents)?\s+only", {"united states", "usa", "american", "u.s."}),
        (r"(?:only\s+)?canadian\s+citizens\s+only", {"canada", "canadian"}),
        (r"(?:only\s+)?uk\s+citizens\s+only", {"united kingdom", "uk", "british"}),
        (r"(?:only\s+)?eu\s+citizens\s+only", {"eu", "european union"}),
    ]
    for pattern, allowed in nationality_rules:
        if re.search(pattern, low):
            reasons_known = True
            if not citizenship:
                return "Unknown"
            if not any(value in citizenship for value in allowed):
                return "Ineligible"

    if any(term in low for term in ["international students are eligible", "open to international applicants", "all nationalities", "international applicants welcome", "citizens of any country"]):
        return "Possibly Eligible"
    # No explicit conflict was found. This is a preliminary screen, not a
    # guarantee; the official programme rules still need human confirmation.
    return "Possibly Eligible" if reasons_known or not low else "Possibly Eligible"


def extract_candidate(result: dict, profile: UserProfile) -> DiscoveredOpportunity | None:
    source_url = canonicalize_url(str(result.get("url", "")))
    if not source_url:
        return None
    page = fetch_page(source_url)
    if page:
        page_title, text, discovered_apply_url = page
    else:
        page_title, text, discovered_apply_url = str(result.get("title", "")), str(result.get("description", "")), None
    title = page_title or str(result.get("title", "Research opportunity"))
    content = (str(result.get("description", "")) + "\n" + text)[:35_000]
    low = content.lower()
    year = profile.intake_year
    # Avoid unrelated content when the page explicitly names a different intake year.
    years = [int(y) for y in re.findall(r"\b20\d{2}\b", content)]
    if years and year not in years and not any(y == year - 1 for y in years):
        return None
    interests = json.loads(profile.interests_json or "[]")
    domains = _infer_domains(content + " " + title, interests)
    score = _relevance(content + " " + title, interests)
    if score < 0.18:
        return None
    funding_type, funding_details, stipend, tuition, travel, housing = _funding(content)
    if profile.funding_preference == "funded_only" and funding_type not in {"Fully funded", "Not disclosed"}:
        return None
    if profile.funding_preference == "funded_and_partial" and funding_type == "Unpaid":
        return None
    deadline, deadline_note = _extract_deadline(content)
    today = date.today()
    if deadline and deadline < today:
        status = "Expired"
    elif deadline:
        status = "Open / Upcoming"
    else:
        status = "Unknown"
    host = urlparse(source_url).hostname or ""
    institution = _infer_institution(title, host, content)
    verification = "Official Source" if _official_domain(source_url) else "Needs Review"
    # Do not infer nationality eligibility or a location from the user's profile.
    country = _extract_country(content)
    eligibility = _eligibility(content, profile)
    # Confirm the target cycle only when the year appears in intake/season
    # context; a copyright footer or unrelated date must not validate the cycle.
    cycle_text = f"{title} {content}".lower()
    seasons = r"summer|winter|spring|autumn|fall"
    target_confirmed = any(re.search(pattern, cycle_text, re.I) for pattern in [
        rf"\b(?:{seasons})\s+{year}\b",
        rf"\b{year}\s+(?:{seasons})\b",
        rf"\b(?:intake|cohort)\s+(?:for\s+)?{year}\b",
        rf"\bfor\s+(?:the\s+)?{year}\s+(?:intake|cohort)\b",
        rf"\b(?:applications?|program|internship|fellowship)\s+for\s+{year}\b",
    ])
    chosen_application_url = canonicalize_url(discovered_apply_url or source_url)
    if not chosen_application_url:
        return None
    if discovered_apply_url is None:
        parsed_source = urlparse(source_url)
        programme_path = (parsed_source.path or "/").strip("/")
        if not _official_domain(source_url) or not programme_path:
            # Do not turn a general directory or an institution homepage into
            # an application link. Third-party results need an explicit portal.
            return None
    try:
        return DiscoveredOpportunity(
            program_name=title[:400], institution=institution, country=country, domains=domains,
            funding_type=funding_type, funding_details=funding_details, stipend=stipend,
            tuition_waiver=tuition, travel_grant=travel, housing=housing,
            deadline=deadline, deadline_note=deadline_note,
            application_url=chosen_application_url, official_source_url=source_url,
            verification_status=verification, intake_year=year, intake_confirmed=target_confirmed,
            eligibility=eligibility, opportunity_status=status, relevance_score=score,
        )
    except Exception as exc:
        logger.debug("Candidate failed validation: %s", type(exc).__name__)
        return None


def discover_for_profile(profile: UserProfile) -> list[DiscoveredOpportunity]:
    if not settings.tavily_api_key:
        logger.warning("Discovery skipped: TAVILY_API_KEY is not configured")
        return []
    searcher = TavilySearch(settings.tavily_api_key)
    seen_urls: set[str] = set()
    candidates: list[DiscoveredOpportunity] = []
    queries = build_queries(profile)
    pages_fetched = 0
    try:
        for query in queries:
            if pages_fetched >= settings.max_pages_per_run:
                break
            try:
                results = searcher.search(query, count=settings.search_results_per_query)
            except httpx.HTTPError as exc:
                logger.warning("Search request failed (%s)", type(exc).__name__)
                continue
            for result in results:
                url = canonicalize_url(str(result.get("url", "")))
                if not url or url in seen_urls or pages_fetched >= settings.max_pages_per_run:
                    continue
                seen_urls.add(url)
                pages_fetched += 1
                candidate = extract_candidate(result, profile)
                if candidate:
                    candidates.append(candidate)
                time.sleep(0.35)
            time.sleep(0.8)
    finally:
        searcher.close()
    return candidates


def persist_candidates(db: Session, user_id: str, candidates: list[DiscoveredOpportunity]) -> tuple[int, int]:
    inserted = 0
    updated = 0
    for candidate in candidates:
        canonical_url = canonicalize_url(str(candidate.official_source_url))
        if not canonical_url:
            continue
        row = db.scalar(select(UserOpportunity).where(
            UserOpportunity.user_id == user_id,
            UserOpportunity.canonical_url == canonical_url,
        ))
        values = {
            "program_name": candidate.program_name,
            "institution": candidate.institution,
            "country": candidate.country,
            "domains_json": json.dumps(candidate.domains),
            "funding_type": candidate.funding_type,
            "funding_details": candidate.funding_details,
            "stipend": candidate.stipend,
            "tuition_waiver": candidate.tuition_waiver,
            "travel_grant": candidate.travel_grant,
            "housing": candidate.housing,
            "deadline": candidate.deadline,
            "deadline_note": candidate.deadline_note,
            "application_url": str(candidate.application_url),
            "official_source_url": str(candidate.official_source_url),
            "eligibility": candidate.eligibility,
            "verification_status": candidate.verification_status,
            "relevance_score": candidate.relevance_score,
            "intake_year": candidate.intake_year,
            "intake_confirmed": candidate.intake_confirmed,
            "opportunity_status": candidate.opportunity_status,
            "last_verified_at": datetime.now(timezone.utc),
        }
        if row:
            for key, value in values.items():
                setattr(row, key, value)
            updated += 1
        else:
            # Do not add already-expired programmes as fresh discoveries. An
            # existing row is retained and updated to Expired for history.
            if candidate.opportunity_status == "Expired":
                continue
            row = UserOpportunity(user_id=user_id, canonical_url=canonical_url, **values)
            db.add(row)
            inserted += 1
    db.commit()
    return inserted, updated
