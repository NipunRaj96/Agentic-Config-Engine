"""
jd_extractor.py
───────────────
Unified Job Description (JD) detail payload extractor.
Decouples detail extraction strategy from listing parser type.

Supports:
  - Direct JSON detail payload -> JDJSON1..N / JDJSONSEQ1..N
  - HTML detail page -> JDRGX1..N / JDRGXSEQ1..N (adaptive windowing + raw execution)
  - Multi-sample cross validation (up to 3 sample jobs)
  - At most 1 targeted self-healing retry
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
import urllib3

from src.extraction.json_parser import JsonParser
from src.extraction.regex_parser import RegexParser
from src.jd_detector import JDDetector
from src.llm_client import LLMClient
from src.models import GeneratorInput

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

_DEFAULT_UNREACHABLE_SIGNALS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "knowledge_base",
    "unreachable_jd_signals.json",
)


@dataclass
class UnreachableJDSignals:
    blocked_schemes_and_prefixes: list[str] = field(default_factory=lambda: [
        "javascript:", "mailto:", "tel:", "whatsapp:", "wa.me/", "#"
    ])
    document_extensions: list[str] = field(default_factory=lambda: [
        ".pdf", ".doc", ".docx", ".rtf", ".zip", ".xlsx", ".xls", ".csv", ".odt", ".ppt", ".pptx"
    ])
    file_sharing_domains: list[str] = field(default_factory=lambda: [
        "drive.google.com", "docs.google.com/document", "docs.google.com/file",
        "dropbox.com", "onedrive.live.com", "sharepoint.com", "box.com"
    ])
    external_form_domains: list[str] = field(default_factory=lambda: [
        "forms.gle", "docs.google.com/forms", "typeform.com", "jotform.com", "surveymonkey.com"
    ])
    unreachable_status_codes: list[int] = field(default_factory=lambda: [
        400, 401, 403, 404, 410, 500, 502, 503, 504
    ])
    blocked_mime_types: list[str] = field(default_factory=lambda: [
        "application/pdf", "application/octet-stream", "application/msword",
        "application/vnd.openxmlformats-officedocument", "application/zip"
    ])

    @classmethod
    def load(cls, path: str = _DEFAULT_UNREACHABLE_SIGNALS_PATH) -> "UnreachableJDSignals":
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return cls(
                    blocked_schemes_and_prefixes=data.get("blocked_schemes_and_prefixes", cls().blocked_schemes_and_prefixes),
                    document_extensions=data.get("document_extensions", cls().document_extensions),
                    file_sharing_domains=data.get("file_sharing_domains", cls().file_sharing_domains),
                    external_form_domains=data.get("external_form_domains", cls().external_form_domains),
                    unreachable_status_codes=data.get("unreachable_status_codes", cls().unreachable_status_codes),
                    blocked_mime_types=data.get("blocked_mime_types", cls().blocked_mime_types),
                )
        except Exception as exc:
            logger.warning("UnreachableJDSignals: failed to load %s (%s), using fallbacks", path, exc)
        return cls()


_JD_FETCH_TIMEOUT = 12
_NOISE_HEADERS = frozenset({
    "cookie", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "upgrade-insecure-requests", "accept-language", "accept-encoding",
    "x-forwarded-for", "x-real-ip", "content-length", "host"
})

_PROMPT_JD_RGX = """\
You are an expert web crawler engineer writing JPERL regular expression configuration for job detail pages.

Career URL: {career_url}
Job Detail URL: {jd_url}

Job Detail Page HTML (focused context window):
---
{windowed_html}
---

TASK:
Write a robust PCRE regex pattern to extract the complete job description (JOBDESC) from this detail page HTML.
If secondary metadata fields (like Date, LOCATION, EXPERIENCE) are also clearly isolated on the detail page, you may provide additional indexed regex patterns.

Return ONLY a valid JSON object matching this schema:
{{
  "strategy": "JDRGX",
  "patterns": [
    {{
      "pattern": "(?s)<div class=\\"job-description\\">(.+?)</div>",
      "seq": "JOBDESC"
    }}
  ],
  "confidence": 0.90
}}

RULES:
1. Primary pattern (patterns[0]) MUST capture the full, complete job description text with sequence "JOBDESC".
2. Start every regex with `(?s)` to enable multiline matching across newlines.
3. Keep the pattern simple and robust:
   - Prefer container-level matching: e.g. `(?s)<div[^>]*class=["'][^"']*description[^"']*["'][^>]*>(.+?)</div>` or `<section[^>]*>(.+?)</section>`
   - Or clean start/end anchors: e.g. `(?s)About the Role:(.+?)Apply Now` or `(?s)Job Description(.+?)<div class="apply"`
4. End the pattern at a reliable closing boundary (e.g. closing tag `</div>`, 'Apply Now' button, or social share container).
5. Never match the entire HTML page body.
6. Escape double quotes and backslashes properly for JSON encoding.
7. Protected JOBDESC: Primary pattern (patterns[0]) MUST capture the COMPLETE description. Secondary patterns (patterns[1..N] for LOCATION, EXPERIENCE, Date) must ONLY target isolated metadata tags/badges outside the main description body. Never slice or extract sub-phrases from inside the primary JOBDESC container.
8. Never force-create LOCATION or EXPERIENCE: If distinct metadata tags do not exist on the detail page, provide ONLY patterns[0] (JOBDESC). Do NOT guess or hallucinate secondary fields.
"""

_PROMPT_JD_JSON = """\
You are an expert web crawler engineer writing JPERL JSON path configuration for job detail API responses.

Career URL: {career_url}
Job Detail API URL: {jd_url}

Detail JSON Response Payload (truncated):
---
{json_snippet}
---

TASK:
Identify the JSON property path(s) that contain the full job description (JOBDESC) and any auxiliary fields (like Date, LOCATION, EXPERIENCE, REF_CODE).

Return ONLY a valid JSON object matching this schema:
{{
  "strategy": "JDJSON",
  "patterns": [
    {{
      "path": "data,jobDescription",
      "seq": "JOBDESC"
    }}
  ],
  "confidence": 0.90
}}

RULES:
1. Primary pattern (patterns[0]) MUST extract the full, complete job description with sequence "JOBDESC".
2. Path syntax: use comma-separated keys for nested properties (e.g. `data,description` or `jobPostingInfo,jobDescription`).
3. For array elements, use the numeric index if fixed (e.g. `sections,0,text`) or relative property path.
4. If multiple description fields exist (e.g. `description` and `requirements`), you may include multiple paths with seq="JOBDESC".
5. Protected JOBDESC: Primary pattern (patterns[0]) MUST capture the COMPLETE description. Secondary patterns for LOCATION/EXPERIENCE must only target dedicated property keys (e.g. `location`, `workExperience`). If not present as dedicated keys, omit them.
"""


_ZOHO_JD_INSTRUCTION = r"""

*** CRITICAL INSTRUCTION FOR ZOHO RECRUIT DETAIL PAGES ***
This detail page contains Zoho Recruit serialized job attributes (e.g. `\x22Job_Description\x22:\x22...<html>...</html>\x22`, `\x22Work_Experience\x22:\x22...\x22`, `\x22City\x22:\x22...\x22` or HTML description).
1. Primary pattern (patterns[0]): Extract the complete Job_Description:
   `(?s)(?:\\x22|")Job_Description(?:\\x22|")\s*:\s*(?:\\x22|")(.+?)(?:\\x22|")\s*,\s*(?:\\x22|")Work_Experience`
   with seq: "JOBDESC"
2. Secondary patterns:
   - If `Work_Experience` is present with a non-null value (e.g. `\x22Work_Experience\x22:\x220\-1 year\x22`), provide an additional pattern:
     `{"pattern": "(?s)(?:\\\\x22|\")Work_Experience(?:\\\\x22|\")\\s*:\\s*(?:\\\\x22|\")([^\"\\\\]+)", "seq": "EXPERIENCE"}`
   - If `City` or `Country` is present with a non-null string value, provide an additional pattern:
     `{"pattern": "(?s)(?:\\\\x22|\")City(?:\\\\x22|\")\\s*:\\s*(?:\\\\x22|\")([^\"\\\\]+)", "seq": "LOCATION"}`
"""


@dataclass
class JDExtractionResult:
    strategy: str = "NONE"                     # "JDRGX" | "JDJSON" | "NONE"
    jdrgx_patterns: list[dict[str, str]] = field(default_factory=list)
    jdjson_patterns: list[dict[str, str]] = field(default_factory=list)
    verified: bool = False
    detail_url: str = ""
    sample_extracted: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    error_msg: Optional[str] = None


class JDExtractor:
    """
    Handles payload acquisition, payload-driven classification,
    adaptive prompting, and deterministic multi-sample validation.
    """

    def __init__(self, llm_client: Optional[LLMClient] = None, signals_path: Optional[str] = None) -> None:
        self._llm = llm_client or LLMClient()
        self._detector = JDDetector.get_instance()
        self._unreachable_signals = UnreachableJDSignals.load(signals_path) if signals_path else UnreachableJDSignals.load()

    def is_unsupported_jd_url(self, url: str) -> tuple[bool, str]:
        """
        Checks if a URL is non-routable, an external document/form, or a file download
        where extracting a standard HTML/JSON JD is impossible.
        """
        if not url or not isinstance(url, str):
            return True, "Empty or invalid URL"
        
        clean_u = url.strip()
        u_lower = clean_u.lower()
        
        # 1. Blocked scheme/prefix check
        for prefix in self._unreachable_signals.blocked_schemes_and_prefixes:
            if u_lower.startswith(prefix.lower()):
                return True, f"Blocked scheme/prefix '{prefix}'"
            
        # 2. Document extension check (ignoring query parameters & hash)
        path_part = u_lower.split("?")[0].split("#")[0]
        for ext in self._unreachable_signals.document_extensions:
            if path_part.endswith(ext.lower()):
                return True, f"Document/file download extension '{ext}'"
                
        # 3. File sharing domain check
        for domain in self._unreachable_signals.file_sharing_domains:
            if domain.lower() in u_lower:
                return True, f"Cloud file sharing domain '{domain}'"
                
        # 4. External form domain check
        for domain in self._unreachable_signals.external_form_domains:
            if domain.lower() in u_lower:
                return True, f"External form domain '{domain}'"
                
        return False, ""

    def extract(
        self,
        inp: GeneratorInput,
        sample_urls: list[str],
        request_headers: Optional[dict[str, str]] = None,
        cached_payloads: Optional[dict[str, str]] = None
    ) -> JDExtractionResult:
        valid_urls: list[str] = []
        for u in (sample_urls or []):
            if u and isinstance(u, str) and u.strip():
                clean_u = u.strip()
                if clean_u.startswith(("http://", "https://")):
                    if clean_u not in valid_urls:
                        valid_urls.append(clean_u)
                elif inp.career_site_url:
                    from urllib.parse import urljoin
                    resolved = urljoin(inp.career_site_url, clean_u)
                    if resolved.startswith(("http://", "https://")) and resolved not in valid_urls:
                        valid_urls.append(resolved)

        if not valid_urls:
            return JDExtractionResult(strategy="NONE", error_msg="No valid detail URLs provided for JD extraction.")

        primary_url = valid_urls[0]
        cached_payloads = cached_payloads or {}

        # 0. Check if primary URL is unsupported/external/file
        is_unsupported, reason = self.is_unsupported_jd_url(primary_url)
        if is_unsupported:
            logger.info("JDExtractor: detail URL %s is unsupported for HTML extraction (%s)", primary_url, reason)
            return JDExtractionResult(
                strategy="NONE",
                detail_url=primary_url,
                error_msg=f"Unsupported detail URL ({reason}): {primary_url}"
            )

        # 1. Fetch Primary Payload
        raw_payload, content_type = self._fetch_payload(primary_url, request_headers, cached_payloads)
        if not raw_payload:
            return JDExtractionResult(
                detail_url=primary_url,
                error_msg=f"Could not fetch detail payload from {primary_url}."
            )

        # 2. Classify Payload
        payload_type = self._detector.detect_payload_type(raw_payload, content_type)
        logger.info("JDExtractor: detail payload for %s classified as '%s' (%d bytes)", primary_url, payload_type, len(raw_payload))

        if payload_type == "JSON":
            return self._handle_json_detail(inp, primary_url, raw_payload, valid_urls, request_headers, cached_payloads)
        else:
            # HTML or HTML_WITH_EMBEDDED_JSON -> evaluate as HTML/JDRGX
            return self._handle_html_detail(inp, primary_url, raw_payload, valid_urls, request_headers, cached_payloads)

    # ── Detail JSON Branch ──────────────────────────────────────────────────────

    def _handle_json_detail(
        self,
        inp: GeneratorInput,
        primary_url: str,
        raw_payload: str,
        all_urls: list[str],
        request_headers: Optional[dict[str, str]],
        cached_payloads: dict[str, str]
    ) -> JDExtractionResult:
        """Processes a direct JSON detail API response using structured subtree extraction."""
        json_snippet, meta = self._detector.distill_and_window_json(raw_payload)
        logger.info(
            "JDExtractor: structured JSON context window for %s -> %d chars (strategy=%s, original=%d)",
            primary_url, len(json_snippet), meta.get("strategy"), meta.get("original_size", len(raw_payload))
        )
        prompt = _PROMPT_JD_JSON.format(
            career_url=inp.career_site_url,
            jd_url=primary_url,
            json_snippet=json_snippet,
        )
        is_zoho = "zohorecruit" in (inp.career_site_url or "").lower() or "zohorecruit" in primary_url.lower()
        if is_zoho:
            prompt += _ZOHO_JD_INSTRUCTION

        raw_llm = self._llm.call(prompt, temperature=0.05)
        parsed_patterns = self._parse_llm_json(raw_llm)

        if not parsed_patterns:
            return JDExtractionResult(
                strategy="JDJSON",
                detail_url=primary_url,
                error_msg="LLM returned no parseable JDJSON patterns."
            )

        # Build config dictionary for validation
        test_config = self._build_jdjson_config(parsed_patterns)
        extracted = JsonParser.execute_jd(test_config, raw_payload)
        is_valid, err = self._detector.validate_jd_text(extracted.get("JOBDESC"), raw_full_text=raw_payload)

        # Cross-validation on secondary URLs (if primary is valid)
        cross_ok, tested_count, passed_count, cross_err = True, 0, 0, ""
        if is_valid:
            cross_ok, tested_count, passed_count, cross_err = self._cross_validate_extractor(
                "JDJSON", test_config, all_urls[1:], request_headers, cached_payloads
            )

        # 1 Self-Healing Retry if primary validation OR cross-validation failed
        if not is_valid or not cross_ok:
            fail_reason = err if not is_valid else f"Pattern overfit to primary page: {cross_err}"
            logger.info("JDExtractor: JDJSON validation/generalization failed (%s). Retrying once...", fail_reason)
            retry_prompt = (
                prompt + f"\n\n*** CRITICAL: PREVIOUS ATTEMPT FAILED: {fail_reason} ***\n"
                "Please output corrected general JDJSON property paths that work consistently across job detail payloads."
            )
            retry_raw = self._llm.call(retry_prompt, temperature=0.05)
            retry_patterns = self._parse_llm_json(retry_raw)
            if retry_patterns:
                retry_cfg = self._build_jdjson_config(retry_patterns)
                retry_extracted = JsonParser.execute_jd(retry_cfg, raw_payload)
                retry_valid, retry_err = self._detector.validate_jd_text(retry_extracted.get("JOBDESC"), raw_full_text=raw_payload)
                if retry_valid:
                    retry_cross_ok, retry_tested, retry_passed, retry_cross_err = self._cross_validate_extractor(
                        "JDJSON", retry_cfg, all_urls[1:], request_headers, cached_payloads
                    )
                    if retry_cross_ok:
                        parsed_patterns = retry_patterns
                        test_config = retry_cfg
                        extracted = retry_extracted
                        is_valid = True
                        cross_ok = True
                        tested_count = retry_tested
                        passed_count = retry_passed
                        err = ""
                    else:
                        cross_ok = False
                        cross_err = retry_cross_err
                else:
                    is_valid = False
                    err = retry_err

        if not is_valid or not cross_ok:
            failure_msg = err if not is_valid else f"Cross-validation generalization failed: {cross_err}"
            return JDExtractionResult(
                strategy="JDJSON",
                jdjson_patterns=parsed_patterns,
                verified=False,
                detail_url=primary_url,
                sample_extracted=extracted,
                error_msg=f"JDJSON validation failed: {failure_msg}"
            )

        confidence = 0.95 if tested_count > 0 else 0.75
        logger.info(
            "JDExtractor: JDJSON verified (confidence=%.2f, tested %d secondary samples, all passed)",
            confidence, tested_count
        )

        return JDExtractionResult(
            strategy="JDJSON",
            jdjson_patterns=parsed_patterns,
            verified=True,
            detail_url=primary_url,
            sample_extracted=extracted,
            confidence=confidence,
        )

    # ── Detail HTML Branch ──────────────────────────────────────────────────────

    def _handle_html_detail(
        self,
        inp: GeneratorInput,
        primary_url: str,
        raw_payload: str,
        all_urls: list[str],
        request_headers: Optional[dict[str, str]],
        cached_payloads: dict[str, str]
    ) -> JDExtractionResult:
        """Processes a server-rendered HTML detail page using adaptive windowing."""
        windowed_html, meta = self._detector.distill_and_window_html(raw_payload)
        logger.info(
            "JDExtractor: adaptive context window for %s -> span %d-%d (%d chars, strategy=%s)",
            primary_url, meta.get("start_idx", 0), meta.get("end_idx", len(windowed_html)),
            len(windowed_html), meta.get("strategy")
        )

        prompt = _PROMPT_JD_RGX.format(
            career_url=inp.career_site_url,
            jd_url=primary_url,
            windowed_html=windowed_html,
        )
        is_zoho = "zohorecruit" in (inp.career_site_url or "").lower() or "zohorecruit" in primary_url.lower() or "Job_Description" in windowed_html
        if is_zoho:
            prompt += _ZOHO_JD_INSTRUCTION

        raw_llm = self._llm.call(prompt, temperature=0.05)
        parsed_patterns = self._parse_llm_json(raw_llm)

        if not parsed_patterns:
            return JDExtractionResult(
                strategy="JDRGX",
                detail_url=primary_url,
                error_msg="LLM returned no parseable JDRGX patterns."
            )

        # Test and validate against RAW FULL UNSTRIPPED HTML
        test_config = self._build_jdrgx_config(parsed_patterns)
        extracted = RegexParser.execute_jd(test_config, raw_payload)
        is_valid, err = self._detector.validate_jd_text(extracted.get("JOBDESC"), raw_full_text=raw_payload)

        # Cross-validation on secondary URLs (if primary is valid)
        cross_ok, tested_count, passed_count, cross_err = True, 0, 0, ""
        if is_valid:
            cross_ok, tested_count, passed_count, cross_err = self._cross_validate_extractor(
                "JDRGX", test_config, all_urls[1:], request_headers, cached_payloads
            )

        # 1 Self-Healing Retry if primary validation OR cross-validation failed
        if not is_valid or not cross_ok:
            fail_reason = err if not is_valid else f"Pattern overfit to primary page: {cross_err}"
            logger.info("JDExtractor: JDRGX validation/generalization failed (%s). Retrying once with diagnostic...", fail_reason)
            retry_prompt = (
                prompt + f"\n\n*** CRITICAL: PREVIOUS REGEX ATTEMPT FAILED ***\n"
                f"Your pattern failed with error: {fail_reason}.\n"
                f"Please generate a general, robust container regex (e.g. `(?s)<div[^>]*class=[^>]*>(.+?)</div>` "
                f"or `(?s)Job Description:(.+?)Apply Now`) that does NOT hardcode specific job IDs, titles, or requisition numbers."
            )
            retry_raw = self._llm.call(retry_prompt, temperature=0.05)
            retry_patterns = self._parse_llm_json(retry_raw)
            if retry_patterns:
                retry_cfg = self._build_jdrgx_config(retry_patterns)
                retry_extracted = RegexParser.execute_jd(retry_cfg, raw_payload)
                retry_valid, retry_err = self._detector.validate_jd_text(retry_extracted.get("JOBDESC"), raw_full_text=raw_payload)
                if retry_valid:
                    retry_cross_ok, retry_tested, retry_passed, retry_cross_err = self._cross_validate_extractor(
                        "JDRGX", retry_cfg, all_urls[1:], request_headers, cached_payloads
                    )
                    if retry_cross_ok:
                        parsed_patterns = retry_patterns
                        test_config = retry_cfg
                        extracted = retry_extracted
                        is_valid = True
                        cross_ok = True
                        tested_count = retry_tested
                        passed_count = retry_passed
                        err = ""
                    else:
                        cross_ok = False
                        cross_err = retry_cross_err
                else:
                    is_valid = False
                    err = retry_err

        if not is_valid or not cross_ok:
            failure_msg = err if not is_valid else f"Cross-validation generalization failed: {cross_err}"
            return JDExtractionResult(
                strategy="JDRGX",
                jdrgx_patterns=parsed_patterns,
                verified=False,
                detail_url=primary_url,
                sample_extracted=extracted,
                error_msg=f"JDRGX validation failed: {failure_msg}"
            )

        confidence = 0.95 if tested_count > 0 else 0.75
        logger.info(
            "JDExtractor: JDRGX verified (confidence=%.2f, tested %d secondary samples, all passed)",
            confidence, tested_count
        )

        return JDExtractionResult(
            strategy="JDRGX",
            jdrgx_patterns=parsed_patterns,
            verified=True,
            detail_url=primary_url,
            sample_extracted=extracted,
            confidence=confidence,
        )

    # ── Helpers & Cross-Validation ──────────────────────────────────────────────

    def _fetch_payload(
        self,
        url: str,
        request_headers: Optional[dict[str, str]],
        cached_payloads: dict[str, str]
    ) -> tuple[Optional[str], Optional[str]]:
        """Fetches payload with caching and clean headers."""
        if url in cached_payloads:
            return cached_payloads[url], None

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NaukriBot/1.0"}
        if request_headers:
            for k, v in request_headers.items():
                if k.lower() not in _NOISE_HEADERS:
                    headers[k] = v

        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=_JD_FETCH_TIMEOUT,
                allow_redirects=True,
                verify=False,
            )
            content_type = resp.headers.get("Content-Type", "")
            
            # Check for unreachable/server error status codes (e.g. 404, 500)
            if resp.status_code in self._unreachable_signals.unreachable_status_codes:
                logger.info("JDExtractor: detail URL %s returned unreachable HTTP status %d", url, resp.status_code)
                return None, None

            # Check for blocked non-HTML MIME types (e.g. application/pdf, binary)
            content_type_lower = content_type.lower()
            for mime in self._unreachable_signals.blocked_mime_types:
                if mime.lower() in content_type_lower:
                    logger.info("JDExtractor: detail URL %s returned non-HTML MIME type '%s'", url, content_type)
                    return None, None

            if resp.status_code == 200 and resp.text:
                cached_payloads[url] = resp.text
                return resp.text, content_type
        except Exception as exc:
            logger.debug("JDExtractor: network fetch failed for %s (%s)", url, exc)

        return None, None

    def _parse_llm_json(self, raw_llm: Optional[str]) -> list[dict[str, str]]:
        """Extracts patterns array from LLM response."""
        if not raw_llm or not raw_llm.strip():
            return []
        try:
            clean = raw_llm.strip()
            s_idx = clean.find("{")
            e_idx = clean.rfind("}")
            if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
                clean = clean[s_idx : e_idx + 1]

            try:
                data = json.loads(clean)
            except Exception:
                # Fallback: escape invalid single backslashes in regex strings
                escaped = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', clean)
                data = json.loads(escaped)

            patterns = data.get("patterns", [])
            if isinstance(patterns, list) and patterns:
                return [p for p in patterns if isinstance(p, dict)]
        except Exception as exc:
            logger.warning("JDExtractor: failed to parse LLM response (%s)", exc)
        return []

    @staticmethod
    def _sanitize_jdrgx_compatibility(pattern: str) -> str:
        """
        Lightweight compatibility audit and sanitization for JPERL target regex engine:
          - Converts Python named capture groups (?P<name>...) to standard indexed groups (...)
          - Strips Python-only inline flags like (?a), (?u), (?L) that break Perl regex engine
          - Validates that pattern has valid syntax and at least 1 capture group
        """
        if not pattern:
            return ""
        pat = pattern.strip()

        # 1. Convert Python named groups (?P<name>...) -> (...)
        pat = re.sub(r'\(\?P<[a-zA-Z0-9_]+>', '(', pat)

        # 2. Strip Python-specific flags (?a), (?u), (?L)
        pat = re.sub(r'\(\?[auL]+\)', '', pat)

        # 3. Ensure valid syntax and contains capture group
        try:
            compiled = re.compile(pat)
            if compiled.groups < 1:
                logger.debug("JDExtractor: JDRGX pattern '%s' has 0 capture groups", pat)
        except Exception as exc:
            logger.warning("JDExtractor: invalid regex pattern syntax '%s': %s", pat, exc)

        return pat

    @staticmethod
    def _build_jdrgx_config(patterns: list[dict[str, str]]) -> dict[str, str]:
        config: dict[str, str] = {}
        for idx, item in enumerate(patterns, 1):
            raw_pat = item.get("pattern", "").strip()
            pat = JDExtractor._sanitize_jdrgx_compatibility(raw_pat)
            seq = item.get("seq", "JOBDESC").strip()
            if pat:
                config[f"JDRGX{idx}"] = pat
                config[f"JDRGXSEQ{idx}"] = seq or "JOBDESC"
        return config

    @staticmethod
    def _build_jdjson_config(patterns: list[dict[str, str]]) -> dict[str, str]:
        config: dict[str, str] = {}
        for idx, item in enumerate(patterns, 1):
            path = item.get("path", "").strip()
            seq = item.get("seq", "JOBDESC").strip()
            if path:
                config[f"JDJSON{idx}"] = path
                config[f"JDJSONSEQ{idx}"] = seq or "JOBDESC"
        return config

    def _cross_validate_extractor(
        self,
        strategy: str,
        config: dict[str, str],
        secondary_urls: list[str],
        request_headers: Optional[dict[str, str]],
        cached_payloads: dict[str, str]
    ) -> tuple[bool, int, int, str]:
        """
        Cross-validates an extractor against secondary detail URLs.
        Returns:
            (passed: bool, tested_count: int, passed_count: int, failure_reason: str)
        """
        tested_count = 0
        passed_count = 0
        failure_reason = ""

        for u in secondary_urls[:2]:
            payload, _ = self._fetch_payload(u, request_headers, cached_payloads)
            if not payload:
                continue

            tested_count += 1
            if strategy == "JDJSON":
                res = JsonParser.execute_jd(config, payload)
            else:
                res = RegexParser.execute_jd(config, payload)

            desc = res.get("JOBDESC")
            is_valid, err = self._detector.validate_jd_text(desc, raw_full_text=payload)
            if is_valid:
                passed_count += 1
                logger.info("JDExtractor: cross-validation PASSED on secondary %s URL %s (%d chars)", strategy, u, len(desc or ""))
            else:
                failure_reason = f"Failed on secondary URL '{u}': {err}"
                logger.warning("JDExtractor: cross-validation FAILED on secondary %s URL %s: %s", strategy, u, err)

        if tested_count == 0:
            # Single-sample valid (no secondary pages available or reachable)
            return True, 0, 0, ""

        if passed_count == tested_count:
            return True, tested_count, passed_count, ""

        return False, tested_count, passed_count, failure_reason
