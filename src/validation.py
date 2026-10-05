import re
import regex
import logging
from typing import Optional
from src.extraction.replay_engine import ReplayEngine

logger = logging.getLogger(__name__)

def check_job_link_description(job_link: str) -> bool:
    """
    Statically fetches the detailed job link page and checks if it contains a valid description.
    """
    if not job_link or not job_link.lower().startswith(("http://", "https://")):
        return False
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        resp = requests.get(job_link, verify=False, timeout=8, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        if resp.status_code == 200:
            text = resp.text.lower()
            keywords = ["responsibilit", "qualification", "requirement", "experience", "skills", "benefit", "description", "role"]
            matches = sum(1 for kw in keywords if kw in text)
            if matches >= 2 and len(text) > 300:
                return True
    except Exception as e:
        logger.warning("check_job_link_description: failed to fetch detailed job page %s: %s", job_link, e)
    return False


import os
import json
from dataclasses import dataclass

def normalize_source_text(s: str) -> str:
    """
    Deterministic normalization pipeline for raw HTML/JSON source content and candidate fields:
      1. HTML entity unescaping (&quot; -> ", &#34; -> ", &#x22; -> ", &amp; -> &, etc.)
      2. Unicode escape decoding (e.g. \u0022 -> ", \x22 -> ")
      3. URL percent-decoding (e.g. %20 -> ' ', %22 -> '"')
      4. Collapsing multiple whitespaces into a single space and lowercasing
    """
    if not s:
        return ""
    import html as html_module
    from urllib.parse import unquote
    
    # 1. HTML entity decode (double decode handles &amp;#34; -> &#34; -> ")
    norm = html_module.unescape(s)
    norm = html_module.unescape(norm)
    
    # 2. URL decode
    try:
        norm = unquote(norm)
    except Exception:
        pass

    # 3. Unicode escape decode
    try:
        norm = norm.encode('utf-8').decode('unicode_escape')
    except Exception:
        pass

    # 4. Collapse whitespace and lower-case
    norm = re.sub(r'\s+', ' ', norm).lower().strip()
    return norm


@dataclass
class ValidationWeights:
    strong_negative_phrase: float = -3.0
    strong_negative_token: float = -3.0
    weak_negative_token: float = -1.0
    multiword_bonus: float = 1.0
    length_penalty: float = -3.0
    format_penalty: float = -3.0

class ValidationConfig:
    def __init__(self):
        self.weights = ValidationWeights()
        self.strong_negative_phrases = [
            "privacy policy", "contact us", "terms of use", "view all jobs",
            "all rights reserved", "copyright", "next page", "previous page",
            "apply now", "learn more", "read more"
        ]
        self.strong_negative_tokens = {
            "login", "register", "privacy", "contact", "menu", "footer", "search",
            "apply", "details"
        }
        self.weak_negative_tokens = {
            "login", "register", "about", "privacy", "contact", "home", "search",
            "support", "faq", "facebook", "twitter", "linkedin", "instagram", "youtube", "pinterest"
        }
        self.job_id_negative_tokens = {
            "javascript:", "onclick", "tab-", "btn-", "item-", "modal", "accordion",
            "collapse", "widget", "content-", "class=", "href=", "true", "false",
            "null", "undefined", "apply", "details"
        }

    def load_from_file(self, file_path: str):
        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Load weights
                w_data = data.get("weights", {})
                self.weights = ValidationWeights(
                    strong_negative_phrase=w_data.get("strong_negative_phrase", -3.0),
                    strong_negative_token=w_data.get("strong_negative_token", -3.0),
                    weak_negative_token=w_data.get("weak_negative_token", -1.0),
                    multiword_bonus=w_data.get("multiword_bonus", 1.0),
                    length_penalty=w_data.get("length_penalty", -3.0),
                    format_penalty=w_data.get("format_penalty", -3.0)
                )
                
                # Load lists
                self.strong_negative_phrases = data.get("strong_negative_phrases", self.strong_negative_phrases)
                self.strong_negative_tokens = set(data.get("strong_negative_tokens", self.strong_negative_tokens))
                self.weak_negative_tokens = set(data.get("weak_negative_tokens", self.weak_negative_tokens))
                self.job_id_negative_tokens = set(data.get("job_id_negative_tokens", self.job_id_negative_tokens))
                logger.info("ValidationConfig: loaded configuration from %s", file_path)
        except Exception as e:
            logger.warning("ValidationConfig: failed to load configuration from %s, using defaults: %s", file_path, e)

# Instantiate global config
config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge_base", "job_keywords.json")
VAL_CONFIG = ValidationConfig()
VAL_CONFIG.load_from_file(config_path)


def score_title(title: str, val_config: ValidationConfig = VAL_CONFIG) -> float:
    """
    Evaluates evidence of a title being a legitimate job description.
    Negative scores represent navigation noise/buttons.
    """
    title_strip = title.strip()
    if not title_strip:
        return val_config.weights.length_penalty
    
    title_lower = title_strip.lower()
    
    # 1. Length heuristic
    if len(title_strip) < 3:
        return val_config.weights.length_penalty

    # 2. Email / Phone formats check
    if "@" in title_lower or re.search(r'\+?\d[\d\s\-]{8,}', title_lower):
        return val_config.weights.format_penalty

    score = 0.0

    # 3. Strong Negative Phrases substring match
    for phrase in val_config.strong_negative_phrases:
        if phrase in title_lower:
            score += val_config.weights.strong_negative_phrase

    # 4. Token-based checks
    # Clean the title of punctuation to get words
    title_clean = re.sub(r'[^\w\s]', ' ', title_lower)
    words = title_clean.split()
    
    # Check for exact matches for strong negative tokens
    if title_lower in val_config.strong_negative_tokens:
        score += val_config.weights.strong_negative_token

    # Check word-level weak negative tokens
    for word in words:
        if word in val_config.weak_negative_tokens:
            score += val_config.weights.weak_negative_token

    # 5. Multiword bonus (offset negative weight if title has 3+ words)
    if len(words) >= 3:
        score += val_config.weights.multiword_bonus

    return score


from enum import Enum


class ValidationScope(str, Enum):
    PAGE    = "PAGE"     # Validates extraction from a single page/batch
    CATALOG = "CATALOG"  # Validates total accumulated extraction across all pages


def validate_job_objects(
    job_objects: list[dict[str, str]],
    expected_count: int,
    raw_source: Optional[str] = None,
    scope: ValidationScope = ValidationScope.PAGE,
) -> tuple[bool, str]:
    """
    Standardized, format-agnostic semantic validator for job objects.
    Enforces Title quality, validates LOCATION/EXPERIENCE/JOBID against raw source to prevent hallucinations,
    and protects JOBDESC completeness.

    Supports two validation scopes:
      - ValidationScope.PAGE (default): Evaluates whether a single page/batch contains a structurally valid
        and plausible batch of jobs (eliminates the total-catalog vs single-page validation trap).
      - ValidationScope.CATALOG: Evaluates the total accumulated job count across all crawled pages against the
        total catalog estimate.
    """
    if not job_objects:
        return False, "No jobs extracted."

    total_jobs = len(job_objects)
    unique_titles = set()
    empty_titles_count = 0
    noise_titles_count = 0
    explicit_job_ids: list[str] = []

    # Prepare normalized raw source for fast substring verification
    norm_raw_source = normalize_source_text(raw_source) if raw_source else ""

    for job in job_objects:
        # 1. Check JOBTITLE
        title = job.get("JOBTITLE", "").strip()
        if not title:
            empty_titles_count += 1
            continue

        unique_titles.add(title.lower())
        
        # Calculate evidence score
        score = score_title(title)
        if score < 0.0:
            noise_titles_count += 1

        # 2. Check JOBID (Validate length, negative noise tokens, and normalized raw source presence)
        job_id = job.get("JOBID", "").strip()
        job_link = job.get("JOBLINK", "").strip()
        if job_id and job_id.lower() not in {"null", "none", ""}:
            explicit_job_ids.append(job_id)
            
            # Determine if this identifier is a URL, path slug, or derived from JOBLINK
            is_url_or_slug = (
                (bool(job_link) and job_id == job_link)
                or job_id.startswith(("http://", "https://", "/", "./", "?"))
                or (("/" in job_id or any(job_id.endswith(ext) for ext in [".html", ".htm", ".php", ".aspx", ".jsp"])) and len(job_id.split()) <= 4)
            )
            max_id_len = 255 if is_url_or_slug else 60
            if len(job_id) > max_id_len:
                return False, f"Extracted JOBID is excessively long ({len(job_id)} chars), likely matched non-ID content."
            
            job_id_lower = job_id.lower()
            if any(tok in job_id_lower for tok in VAL_CONFIG.job_id_negative_tokens) or any(c in job_id for c in ["<div", "<button", "<script", "<span", "<table", "<p", "{", "}", "\n"]):
                return False, f"Extracted JOBID contains invalid UI or JS noise: '{job_id[:30]}'."
            
            # If JOBID is distinct from JOBTITLE and not a domain-prefixed link, verify existence in normalized raw source
            if job_id.lower() != title.lower() and norm_raw_source:
                target_check = job_id
                if is_url_or_slug and "://" in job_id:
                    try:
                        from urllib.parse import urlparse
                        target_check = urlparse(job_id).path or job_id
                    except Exception:
                        target_check = job_id
                norm_id = normalize_source_text(target_check)
                if norm_id and norm_id not in norm_raw_source:
                    return False, f"Extracted JOBID '{job_id}' not found in normalized raw source (hallucination detected)."

        # 3. Check LOCATION (Validate against normalized raw source, catch hallucinations)
        loc = job.get("LOCATION", "").strip()
        if loc and loc.lower() not in {"null", "none", ""}:
            if len(loc) > 100:
                return False, f"Extracted LOCATION is excessively long ({len(loc)} chars), likely matched non-location text."
            if loc.lower().startswith(("http://", "https://")) or "<div" in loc.lower() or "<script" in loc.lower():
                return False, f"Extracted LOCATION contains invalid URL or HTML markup: '{loc[:30]}'."
            if norm_raw_source:
                norm_loc = normalize_source_text(loc)
                if norm_loc and norm_loc not in norm_raw_source:
                    return False, f"Extracted LOCATION '{loc}' not found in raw source (hallucination detected)."

        # 4. Check EXPERIENCE (Validate against normalized raw source, catch sliced paragraphs)
        exp = job.get("EXPERIENCE", "").strip()
        if exp and exp.lower() not in {"null", "none", ""}:
            if len(exp) > 120:
                return False, f"Extracted EXPERIENCE is too long ({len(exp)} chars), likely sliced from a job description paragraph."
            if "<div" in exp.lower() or "{" in exp or "}" in exp:
                return False, f"Extracted EXPERIENCE contains invalid markup/JSON syntax: '{exp[:30]}'."
            if norm_raw_source:
                norm_exp = normalize_source_text(exp)
                if norm_exp and norm_exp not in norm_raw_source:
                    return False, f"Extracted EXPERIENCE '{exp}' not found in raw source (hallucination detected)."

        # 5. Protect JOBDESC (Hard regression check)
        desc = job.get("JOBDESC", "").strip()
        if desc and len(desc) < 30 and total_jobs <= 3:
            return False, f"Extracted JOBDESC is abnormally short ({len(desc)} chars), likely truncated."

    # Check if all jobs share an identical non-title JOBID (likely static container/class ID)
    if total_jobs > 1 and explicit_job_ids and len(explicit_job_ids) == total_jobs:
        first_id = explicit_job_ids[0].lower()
        if all(jid.lower() == first_id for jid in explicit_job_ids) and first_id not in {t.lower() for t in unique_titles}:
            return False, f"All matched elements share the identical non-title JOBID: '{explicit_job_ids[0]}'. Likely matching a static layout identifier."

    # 1. Reject if too many empty titles
    if empty_titles_count > 0:
        if empty_titles_count / total_jobs > 0.30 or empty_titles_count == total_jobs:
            return False, f"Too many empty job titles: {empty_titles_count} out of {total_jobs}."

    # 2. Reject if too many noise/navigation titles (e.g. more than 30% noise indicates matching a form/footer)
    if noise_titles_count > 0:
        if noise_titles_count / total_jobs > 0.30 or noise_titles_count == total_jobs:
            return False, f"Too many noise or navigation keywords matched: {noise_titles_count} out of {total_jobs}."

    # 3. Duplicate Detection:
    # If all matches have the exact same title and we have more than 1 match, reject.
    if len(unique_titles) == 1 and total_jobs > 1:
        return False, f"All matched elements have the identical title: '{list(unique_titles)[0]}'. Likely matching static layout element."

    # If unique title ratio is extremely low (e.g. less than 30% unique for larger lists)
    if total_jobs >= 5:
        unique_ratio = len(unique_titles) / total_jobs
        if unique_ratio < 0.30:
            return False, f"Duplicate job titles ratio is too high: only {len(unique_titles)} unique out of {total_jobs}."

    # 4. Scope-specific count validation
    if scope == ValidationScope.CATALOG:
        # ── CATALOG Scope: validates total accumulated jobs across full crawl ────
        if expected_count > 0:
            min_allowed = max(2, int(expected_count * 0.40))
            if total_jobs < min_allowed:
                return False, f"Extracted only {total_jobs} total job(s) across catalog but expected {expected_count} jobs."
            if total_jobs > max(50, expected_count * 3):
                return False, f"Extracted total job count ({total_jobs}) is excessively higher than expected ({expected_count})."
        else:
            if total_jobs < 1:
                return False, "Extracted 0 jobs."
    else:
        # ── PAGE Scope: validates single-page batch plausibility & quality ─────
        if expected_count > 0:
            # When the expected catalog is small (<= 6 jobs), all jobs are expected on the single page
            if expected_count <= 6:
                min_allowed = max(1, int(expected_count * 0.50))
                if total_jobs < min_allowed:
                    return False, f"Extracted only {total_jobs} job(s) but expected {expected_count} jobs on single page."
            else:
                # When expected_count > 6, the site is likely paginated or batched
                # Reject if only 1 job matched on a site with >= 5 expected jobs
                if expected_count >= 5 and total_jobs <= 1:
                    return False, f"Extracted only {total_jobs} job(s) from page on a site with {expected_count} expected jobs. Likely a false positive match on layout header/footer."

                # Reject if only <= 2 jobs matched on a large catalog (>= 20 expected jobs)
                if expected_count >= 20 and total_jobs <= 2:
                    return False, f"Extracted only {total_jobs} job(s) from page on a site with {expected_count} expected jobs. Stray false positive match."

                # High quality extraction: >= 3 jobs, zero noise, zero empty, and high title uniqueness (>= 80%)
                is_high_quality_batch = (
                    total_jobs >= 3
                    and noise_titles_count == 0
                    and empty_titles_count == 0
                    and (len(unique_titles) / total_jobs >= 0.80)
                )

                if not is_high_quality_batch:
                    # Non-high-quality batch requires at least 25% of expected or min 3 jobs
                    min_allowed = max(3, int(expected_count * 0.25))
                    if total_jobs < min_allowed:
                        return False, f"Extracted only {total_jobs} job(s) with lower quality metrics (expected {expected_count} jobs or high-quality clean batch)."

            # Reject if page batch is excessively high (e.g. > max(60, expected_count * 4))
            if total_jobs > max(60, expected_count * 4):
                return False, f"Extracted job count ({total_jobs}) is excessively higher than expected single-page batch ({expected_count})."
        else:
            # If expected count is not specified (0), we ensure at least 1 job is matched
            if total_jobs < 1:
                return False, "Extracted 0 jobs."

    return True, ""


def validate_catalog_coverage(
    job_objects: list[dict[str, str]],
    expected_count: int,
) -> tuple[bool, str]:
    """Validates total accumulated jobs across all pages in a completed crawl."""
    return validate_job_objects(job_objects, expected_count, scope=ValidationScope.CATALOG)


def classify_failure(tech_comments: str, error_reason: str) -> str:
    """
    Categorizes pipeline failures into predefined business categories.
    """
    combined = (str(tech_comments) + " " + str(error_reason)).lower()
    if any(code in combined for code in ["401", "403", "404", "unauthorized", "forbidden"]):
        return "HTTP 401/403/404"
    if any(word in combined for word in ["timeout", "unreachable", "connection", "dns", "refused"]):
        return "Site timeout / Temporary network issue"
    if any(word in combined for word in ["duplicate", "noise", "identical title", "too many noise"]):
        return "Duplicate or noisy data"
    if any(word in combined for word in ["missing required", "jobtitle from any item", "empty job titles"]):
        return "Missing required fields"
    if any(word in combined for word in ["api selection failed", "did not yield an array", "resolved jobs list is empty"]):
        return "Wrong API selected"
    if any(word in combined for word in ["syntax", "invalid xpath", "invalid regex", "failed replay validation"]):
        return "Wrong JSON path / Regex / XPath"
    return "Unsupported website structure"


# ── Backward Compatible Validation Wrappers ───────────────────────────────

def validate_regex_jobs(
    locrgx: str,
    locrgxseq: str,
    html: str,
    expected_count: int,
    scope: ValidationScope = ValidationScope.PAGE,
) -> tuple[bool, str, list[dict]]:
    """
    Replays Regex extraction through ReplayEngine and validates.
    """
    try:
        # Check capture group count consistency with regex C engine
        compiled = regex.compile(locrgx, regex.DOTALL)
        fields = [f.strip() for f in locrgxseq.split(",") if f.strip()]
        if compiled.groups != len(fields):
            return False, f"Regex capture groups count ({compiled.groups}) does not match fields count in LOCRGXSEQ ({len(fields)}).", []
    except Exception as e:
        return False, f"Invalid regex pattern: {e}", []

    config = {"LOCRGX": locrgx, "LOCRGXSEQ": locrgxseq}
    try:
        job_list = ReplayEngine.run(config, page_html=html)
        is_valid, err_msg = validate_job_objects(job_list, expected_count, raw_source=html, scope=scope)
        return is_valid, err_msg, job_list
    except TimeoutError:
        return False, "Regex execution timed out (>2.0s) due to potential catastrophic backtracking (ReDoS). Please simplify the regex and avoid chained wildcards.", []
    except Exception as e:
        return False, f"Regex validation exception: {e}", []


def validate_xpath_jobs(
    page_html: str,
    xpath: str,
    expected_count: int,
    scope: ValidationScope = ValidationScope.PAGE,
) -> tuple[bool, str, list[dict]]:
    """
    Replays XPath extraction through ReplayEngine and validates.
    """
    config = {"xpath": xpath}
    try:
        job_list = ReplayEngine.run(config, page_html=page_html)
        is_valid, err_msg = validate_job_objects(job_list, expected_count, raw_source=page_html, scope=scope)
        return is_valid, err_msg, job_list
    except Exception as e:
        return False, f"XPath validation exception: {e}", []

