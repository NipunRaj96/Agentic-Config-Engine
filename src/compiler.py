"""
compiler.py
────────────
Converts structured pipeline results into production-ready legacy JPERL
configuration dicts.

Three input paths:

  Path A — ATS Template  (from ATSFingerprinter)
    Fills a minimal parent-rule config with company metadata +
    tenant-specific URL_VARS / URLSTART / LANDINGJOBLINK.

  Path B — LLM Extraction  (from LLMReasoner)
    Builds a full JPERL config from the extracted API URL, method,
    headers, body, pagination, and field paths (LOCJSON).

  Path C — LOCRGX  (from LOCRGXGenerator)  ★ NEW
    Builds JPERL config with LOCRGX/LOCRGXSEQ regex fields.
    Handles POST (WP admin-ajax), GET (custom URL), and direct career page.

  Path D — XPath SRP  (from XPathSRPGenerator)  ★ NEW
    Builds SRPAUTOMATION XPath JSON schema.

Fixes in v3:
  - from_locrgx(): LOCRGX path with POST body, custom URL, JDRGX support
  - from_xpath_srp(): XPath JSON schema matching OMS ground truth format
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse, urljoin, unquote

from src.config import JPERL_DEFAULTS
from src.models import (
    ATSMatch,
    GeneratorInput,
    JperlConfig,
    LLMExtractionResult,
    LOCRGXResult,
    XPathSRPResult,
    CapturedRequest,
    JDStrategyResult,
    JobLinkEvidence,
)

logger = logging.getLogger(__name__)

_NOISE_HEADERS = frozenset({
    "cookie", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "upgrade-insecure-requests", "accept-language", "accept-encoding",
    "x-forwarded-for", "x-real-ip", "content-length", "host",
    "connection", "priority", "user-agent",
})


class Compiler:
    """
    Translates pipeline outputs into legacy JPERL JSON configs.
    """

    # ── ATS Template path ───────────────────────────────────────────────────────

    def from_ats(self, inp: GeneratorInput, ats: ATSMatch) -> JperlConfig:
        """Build a parent-rule JPERL config from ATS fingerprint data."""
        body: dict[str, Any] = {
            "PARENT_RULE_NAME": ats.parent_rule_name,
            "POSTQUERY": self._build_postquery(inp),
        }

        if ats.url_vars:
            body["URL_VARS"] = ats.url_vars

        if ats.url_start:
            body["URLSTART"] = ats.url_start

        # Merge platform-specific extras (skip NOTE fields — human-readable only)
        for k, v in ats.extra_fields.items():
            if k.endswith("_NOTE") or k.endswith("_REQUIRED"):
                continue
            body[k] = v

        logger.info(
            "Compiler (ATS): compiled %s config for site_id=%s",
            ats.parent_rule_name, inp.site_id,
        )
        return JperlConfig(site_id=inp.site_id, body=body)

    # ── LLM Extraction path ─────────────────────────────────────────────────────

    def from_llm(
        self,
        inp: GeneratorInput,
        result: LLMExtractionResult,
        candidate: Optional[CapturedRequest] = None,
        jd_strategy: Optional[JDStrategyResult] = None,
        enforce_jd_consistency: bool = True
    ) -> JperlConfig:
        """Build a full JPERL config from LLM extraction output."""
        body: dict[str, Any] = copy.deepcopy(JPERL_DEFAULTS)

        # ── URL (with method, body, headers in legacy syntax) ──────────────────
        body["URL"] = self._build_url_field(result)

        # ── JOBLINK ───────────────────────────────────────────────────────────
        joblink = self._build_joblink(result, inp.career_site_url, candidate, jd_strategy)
        if joblink:
            body["JOBLINK"] = joblink

        # ── LANDINGJOBLINK (same as JOBLINK for most custom configs) ──────────
        if joblink:
            body["LANDINGJOBLINK"] = joblink

        # ── MOVE_TO_JD from LLM notes or result ──────────────────────────────
        move_to_jd = getattr(result, "move_to_jd", None)
        if move_to_jd is None:
            move_to_jd = self._extract_move_to_jd(result)
        body["MOVE_TO_JD"] = move_to_jd

        # ── Detail extraction patterns (JDJSON1..N / JDRGX1..N) ────────────────
        self._inject_jd_patterns(
            body,
            jdrgx_patterns=getattr(result, "jdrgx_patterns", None),
            jdjson_patterns=getattr(result, "jdjson_patterns", None),
            legacy_jdrgx=getattr(result, "jdrgx", None),
            legacy_jdrgxseq=getattr(result, "jdrgxseq", None)
        )

        # ── LOCJSON field mappings ────────────────────────────────────────────
        json_mappings = self._build_locjson_mappings(result, candidate)
        if json_mappings:
            body.update(json_mappings)
        else:
            body["LOCRGX"] = ""
            body["LOCRGXSEQ"] = ""

        # ── Enforce JOBLINK extraction consistency ────────────────────────────
        # Invariant: if JOBLINK template contains {{VARJOBLINK}},
        # LOCJSONSEQ must contain JOBLINK. Ensures the extractor and template
        # are always self-consistent. Fails closed with a diagnostic if provenance
        # cannot be established.
        self._enforce_joblink_extraction_consistency(body, result)

        # ── Pagination ────────────────────────────────────────────────────────
        body["MAXPAGESPARSE"] = "10"

        # ── POSTQUERY ─────────────────────────────────────────────────────────
        body["POSTQUERY"] = self._build_postquery(inp)

        # ── Enforce MOVE_TO_JD detail extractor consistency ──────────────────
        if enforce_jd_consistency:
            self._enforce_move_to_jd_consistency(body, inp.site_id)

        logger.info(
            "Compiler (LLM): compiled full JPERL config for site_id=%s (confidence=%.2f)",
            inp.site_id, result.confidence,
        )
        return JperlConfig(site_id=inp.site_id, body=body)

    # ── LOCRGX path ────────────────────────────────────────────────────────────

    def from_locrgx(
        self,
        inp: GeneratorInput,
        result: LOCRGXResult,
        page_html: Optional[str] = None,
        raw_static_html: Optional[str] = None,
        jd_strategy: Optional[JDStrategyResult] = None
    ) -> JperlConfig:
        """
        Build a LOCRGX-based JPERL config.

        URL field construction:
          - If AJAX/custom URL: prepend URL, add {{POST}} body if POST method, add headers
          - If career page (source_url=None): no URL field (crawler uses career_site_url)
        """
        body: dict[str, Any] = copy.deepcopy(JPERL_DEFAULTS)

        if result.locjson and result.locjsonseq:
            locjson = result.locjson.strip()
            locjsonseq = result.locjsonseq.strip()
            # Architectural Invariant: Ensure JOBID is present in LOCJSONSEQ
            json_fields = [f.strip() for f in locjsonseq.split(",") if f.strip()]
            if json_fields and "JOBID" not in [f.split("=")[0] for f in json_fields]:
                if any(f.startswith("JOBLINK") for f in json_fields):
                    jl_field = [f for f in json_fields if f.startswith("JOBLINK")][0]
                    jl_path = jl_field.split("=")[1] if "=" in jl_field else ""
                    json_fields.append(f"JOBID={jl_path}" if jl_path else "JOBID")
                elif any(f.startswith("JOBTITLE") for f in json_fields):
                    jt_field = [f for f in json_fields if f.startswith("JOBTITLE")][0]
                    jt_path = jt_field.split("=")[1] if "=" in jt_field else ""
                    json_fields.append(f"JOBID={jt_path}" if jt_path else "JOBID")
                locjsonseq = ",".join(json_fields)

            body["LOCJSON"] = locjson
            body["LOCJSONSEQ"] = locjsonseq
            if "LOCRGX" in body:
                del body["LOCRGX"]
            if "LOCRGXSEQ" in body:
                del body["LOCRGXSEQ"]
        else:
            locrgx = (result.locrgx or "").strip()
            locrgxseq = (result.locrgxseq or "").strip()

            locrgx, locrgxseq = self._ensure_locrgx_jobid(locrgx, locrgxseq)

            body["LOCRGX"] = locrgx
            body["LOCRGXSEQ"] = locrgxseq
            # Remove LOCJSON fields (not used in LOCRGX path)
            for k in list(body.keys()):
                if k.startswith("LOCJSON"):
                    del body[k]

        # URL field (use custom endpoint if present, otherwise default to careers page URL)
        if result.source_url:
            url_parts = [result.source_url]
            if result.method.upper() == "POST" and result.request_body:
                url_parts.append(f"{{{{POST}}}}{result.request_body}")
            # Only include meaningful headers (skip noise)
            clean_headers = {
                k: v for k, v in result.request_headers.items()
                if k.lower() not in _NOISE_HEADERS
            }
            header_str = self._build_header_string(clean_headers)
            if header_str:
                url_parts.append(header_str)
            body["URL"] = "".join(url_parts)
        else:
            body["URL"] = inp.career_site_url

        body["MOVE_TO_JD"]   = result.move_to_jd
        import math
        if inp.jobs_on_career_page > 0:
            per_page = int(body.get("MAXJBSPPAGE") or 10)
            pages = math.ceil(inp.jobs_on_career_page / per_page) + 5
            body["MAXPAGESPARSE"] = str(pages)
        else:
            body["MAXPAGESPARSE"] = result.max_pages

        # ── Detail extraction patterns (JDRGX1..N / JDJSON1..N) ────────────────
        self._inject_jd_patterns(
            body,
            jdrgx_patterns=getattr(result, "jdrgx_patterns", None),
            jdjson_patterns=getattr(result, "jdjson_patterns", None),
            legacy_jdrgx=result.jdrgx,
            legacy_jdrgxseq=result.jdrgxseq
        )

        # Compile JOBLINK template
        raw_val = ""
        fields = [f.strip() for f in (result.locrgxseq or "").split(",") if f.strip()]
        if result.locrgx and fields:
            try:
                # Try finding a match in raw_static_html or page_html
                search_targets = [t for t in [raw_static_html, page_html] if t]
                for target in search_targets:
                    if not target:
                        continue
                    html_clean = re.sub(r'(?s)<!--.*?-->', '', target)
                    match = re.search(result.locrgx, html_clean)
                    if match:
                        if "JOBLINK" in fields:
                            jl_idx = fields.index("JOBLINK")
                            if match.groups() and jl_idx < len(match.groups()):
                                raw_val = match.group(jl_idx + 1).strip()
                                break
                        elif "JOBID" in fields:
                            jid_idx = fields.index("JOBID")
                            if match.groups() and jid_idx < len(match.groups()):
                                raw_val = match.group(jid_idx + 1).strip()
                                break
            except Exception as e:
                logger.debug("Compiler: failed to parse raw joblink value using regex: %s", e)

        # Fallback to checking jobs_sample
        if not raw_val and hasattr(result, "jobs_sample") and result.jobs_sample:
            raw_val = result.jobs_sample[0].get("JOBLINK", "") or result.jobs_sample[0].get("JOBID", "")

        # If still no raw_val but we have evidences, let resolve_joblink_template align them
        if not raw_val and jd_strategy and jd_strategy.verified and jd_strategy.job_link_evidences:
            raw_val = jd_strategy.job_link_evidences[0].raw_url

        if result.move_to_jd == 1:
            joblink = ""
            if "JOBLINK" in fields or "JOBID" in fields:
                if raw_val.isdigit() and len(raw_val) >= 10 and ("zohorecruit" in inp.career_site_url or "jobs/Careers" in inp.career_site_url):
                    joblink = f"{inp.career_site_url}/{{{{VARJOBLINK}}}}"
                elif raw_val:
                    joblink = self._resolve_joblink_template(raw_val, inp.career_site_url, jd_strategy)
            
            # If joblink was resolved and contains {{VARJOBLINK}}, use it; otherwise provide domain template fallback
            if joblink and "{{VARJOBLINK}}" in joblink:
                body["JOBLINK"] = joblink
            elif raw_val and "/" in str(raw_val) and not str(raw_val).startswith("http"):
                body["JOBLINK"] = f"{inp.career_site_url.rstrip('/')}/{{{{VARJOBLINK}}}}"
            else:
                body["JOBLINK"] = f"{inp.career_site_url}/{{{{VARJOBLINK}}}}"
            body["LANDINGJOBLINK"] = body["JOBLINK"]
        else:
            body["JOBLINK"] = inp.career_site_url
            body["LANDINGJOBLINK"] = inp.career_site_url

        body["POSTQUERY"] = self._build_postquery(inp)

        # ── Enforce MOVE_TO_JD detail extractor consistency ──────────────────
        self._enforce_move_to_jd_consistency(body, inp.site_id)

        logger.info(
            "Compiler (LOCRGX): compiled regex config for site_id=%s (move_to_jd=%d)",
            inp.site_id, result.move_to_jd,
        )
        return JperlConfig(site_id=inp.site_id, body=body)

    # ── XPath SRP path ─────────────────────────────────────────────────────────

    def from_xpath_srp(self, inp: GeneratorInput, result: XPathSRPResult) -> JperlConfig:
        """
        Build XPath-SRP config for SRPAUTOMATION crawler.
        Schema matches ground truth from OMS Activity.csv.
        """
        body: dict[str, Any] = {
            "xpath": result.xpath,
            "isOnlyTextSrp": result.is_only_text_srp,
            "option": False,
            "navigationMethod": result.navigation_method,
            "isNavigationMethodSet": "false",
            "isNextFound": result.is_next_found,
            "loadMore": {
                "xpath": result.load_more_xpath or "",
                "threshold": 100,
            },
            "POSTQUERY": self._build_postquery(inp),
        }
        logger.info(
            "Compiler (XPath-SRP): compiled SRP config for site_id=%s xpath='%s'",
            inp.site_id, result.xpath,
        )
        return JperlConfig(site_id=inp.site_id, body=body)

    # ── Legacy syntax builders ──────────────────────────────────────────────────

    def _build_url_field(self, result: LLMExtractionResult) -> str:
        """
        Compose the JPERL URL field:
          <api_url>[{{POST}}{{CONTENT}}<body>][{{HEADER}}k|X|v##{{k|X|v}]
        """
        parts = [result.api_url]

        if result.method.upper() == "POST" and result.request_body_template:
            body = self._inject_pagination_placeholder(result)
            parts.append(f"{{{{POST}}}}{{{{CONTENT}}}}{body}")

        header_str = self._build_header_string(result.request_headers)
        if header_str:
            parts.append(header_str)

        return "".join(parts)

    def _inject_pagination_placeholder(self, result: LLMExtractionResult) -> str:
        """Replace PAGINATION_PLACEHOLDER with the correct JPERL token."""
        body = result.request_body_template or ""
        pg = result.pagination

        if pg.type == "offset":
            token = "!0o!STARTJOBNO!0o!"
        elif pg.type == "page":
            token = "!0o!CURPG!0o!"
        else:
            token = ""

        return body.replace("PAGINATION_PLACEHOLDER", token)

    def _build_header_string(self, headers: dict[str, str]) -> str:
        """
        Convert headers to JPERL format:
          {{HEADER}}Key1|X|Value1##{{Key2|X|Value2
        """
        if not headers:
            return ""
        items = list(headers.items())
        first_key, first_val = items[0]
        parts = [f"{{{{HEADER}}}}{first_key}|X|{first_val}"]
        for key, val in items[1:]:
            parts.append(f"{{{{{key}|X|{val}")
        return "##".join(parts)

    def _align_joblink_template(self, evidences: list[JobLinkEvidence]) -> Optional[str]:
        """
        Derive JPERL JOBLINK template using trailing segments alignment.
        """
        templates = []
        for ev in evidences:
            raw = ev.raw_url.strip()
            detail = ev.detail_page_url.strip()
            if not raw or not detail:
                continue

            # First check direct substring alignment
            idx = detail.rfind(raw)
            if idx != -1:
                prefix = detail[:idx]
                suffix = detail[idx + len(raw):]
                templates.append((prefix, suffix))
                continue

            # Fallback: segment alignment for redirects
            # Split by '/' but preserve protocol double slash
            raw_parts = raw.split("/")
            detail_parts = detail.split("/")

            # Find matching trailing segments
            match_count = 0
            while (match_count < len(raw_parts) and 
                   match_count < len(detail_parts) and 
                   raw_parts[-1 - match_count] == detail_parts[-1 - match_count] and
                   raw_parts[-1 - match_count] != ""):
                match_count += 1

            if match_count > 0:
                # Reconstruct prefixes
                detail_prefix_parts = detail_parts[:-match_count]
                detail_prefix = "/".join(detail_prefix_parts)
                templates.append((detail_prefix, ""))
            else:
                # Fallback: unquote normalize direct check
                raw_dec = unquote(raw)
                detail_dec = unquote(detail)
                idx = detail_dec.rfind(raw_dec)
                if idx != -1:
                    prefix = detail_dec[:idx]
                    suffix = detail_dec[idx + len(raw_dec):]
                    templates.append((prefix, suffix))

        if not templates:
            return None

        # Ensure consistency across all samples
        first_prefix, first_suffix = templates[0]
        for prefix, suffix in templates[1:]:
            if prefix != first_prefix or suffix != first_suffix:
                logger.warning(
                    "Compiler: Job link templates are inconsistent across samples: %s vs %s",
                    (first_prefix, first_suffix),
                    (prefix, suffix)
                )
                return None

        return f"{first_prefix}{{{{VARJOBLINK}}}}{first_suffix}"

    def _resolve_joblink_template(
        self,
        raw_val: str,
        source_page_url: str,
        jd_strategy: Optional[JDStrategyResult] = None
    ) -> Optional[str]:
        raw_val = (raw_val or "").strip()
        if not raw_val:
            return None

        # Level 1: Authoritative JobLinkEvidence (Strong Evidence)
        if jd_strategy and jd_strategy.verified and jd_strategy.job_link_evidences:
            # If strategy is verified, we must return only what evidence aligned, or fail closed
            tmpl = self._align_joblink_template(jd_strategy.job_link_evidences)
            if tmpl:
                # If raw_val is relative (does not start with http/https), but tmpl is bare {{VARJOBLINK}}
                # because the DOM evidence captured already-resolved absolute URLs, fall through to prefix resolution.
                if not raw_val.lower().startswith(("http://", "https://")) and tmpl.strip() == "{{VARJOBLINK}}":
                    pass
                else:
                    return tmpl
            else:
                return None

        # Base domain resolution
        try:
            parsed = urlparse(source_page_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            base = ""

        # Level 2: Observed Absolute URL
        if raw_val.lower().startswith(("http://", "https://", "mailto:", "tel:")):
            return "{{VARJOBLINK}}"

        # Level 3: Controlled URL Resolution Fallback
        if raw_val.startswith("/"):
            return f"{base}{{{{VARJOBLINK}}}}" if base else None

        # Reject ambiguous query-params/extensions on unverified strategy (Fail Closed)
        if "?" in raw_val:
            logger.debug("Compiler: rejecting relative query-param '%s' (fail-closed)", raw_val)
            return None
        if "." in raw_val:
            allowed_extensions = {".html", ".htm", ".php", ".aspx", ".asp", ".jsp"}
            has_allowed_ext = any(raw_val.lower().endswith(ext) for ext in allowed_extensions)
            if not has_allowed_ext:
                logger.debug("Compiler: rejecting relative extension '%s' (fail-closed)", raw_val)
                return None

        # Standard URL join for simple relative IDs/UUIDs/Slugs
        if base and source_page_url:
            clean_source = source_page_url.split("?")[0].split("#")[0]
            try:
                resolved = urljoin(clean_source, raw_val)
                idx = resolved.rfind(raw_val)
                if idx != -1:
                    prefix = resolved[:idx]
                    suffix = resolved[idx + len(raw_val):]
                    return f"{prefix}{{{{VARJOBLINK}}}}{suffix}"
            except Exception:
                pass

        return None

    def _build_joblink(
        self,
        result: LLMExtractionResult,
        career_url: str,
        candidate: Optional[CapturedRequest] = None,
        jd_strategy: Optional[JDStrategyResult] = None
    ) -> Optional[str]:
        """
        Extract the raw joblink value from candidate or jobs_sample,
        then resolve it using the centralized resolver.
        """
        # Determine the key path field to extract raw value
        field_to_use = result.field_joblink or result.field_jobid

        raw_val = ""
        if candidate and candidate.response_body and field_to_use:
            try:
                import json
                from src.extraction.json_parser import JsonParser
                from src.extraction.candidate_replayer import CandidateReplayer
                data = json.loads(candidate.response_body.strip())
                jobs_array = CandidateReplayer._find_largest_dict_array(data)
                if jobs_array and isinstance(jobs_array[0], dict):
                    clean_path = field_to_use.split("|XX|")[0].replace("|X|", ".")
                    val = JsonParser._get_json_value(jobs_array[0], clean_path)
                    if val is not None:
                        raw_val = str(val).strip()
            except Exception as e:
                logger.debug("Compiler: failed to parse raw joblink value from candidate: %s", e)

        # Fallback to checking jobs_sample
        if not raw_val and field_to_use and hasattr(result, "jobs_sample") and result.jobs_sample:
            try:
                clean_path = field_to_use.split("|XX|")[0].replace("|X|", ".")
                val = result.jobs_sample[0].get(clean_path) or result.jobs_sample[0].get(field_to_use)
                if val is not None:
                    raw_val = str(val).strip()
            except Exception:
                pass

        # If still no raw_val but we have evidences, let resolve_joblink_template align them
        if not raw_val and jd_strategy and jd_strategy.verified and jd_strategy.job_link_evidences:
            raw_val = jd_strategy.job_link_evidences[0].raw_url

        return self._resolve_joblink_template(raw_val, career_url, jd_strategy)

    def _extract_move_to_jd(self, result: LLMExtractionResult) -> int:
        """
        Read MOVE_TO_JD from LLM notes field.
        Default = 1 (need to visit JD page) unless LLM explicitly said 0.
        """
        notes = result.notes or ""
        m = re.search(r"MOVE_TO_JD\s*=\s*([01])", notes)
        if m:
            return int(m.group(1))
        # Also check field_jobdesc as secondary signal
        return 0 if result.field_jobdesc else 1

    def _build_locjson_mappings(self, result: LLMExtractionResult, candidate: Optional[CapturedRequest] = None) -> dict[str, Any]:
        """
        Generate JPERL legacy LOCJSON and LOCJSONSEQ format:
        e.g., LOCJSON = "skills|X|location|X|title|X|slug|X|slug|XX|career_list"
              LOCJSONSEQ = "JOBDESC,LOCATION,JOBTITLE,JOBID,JOBLINK"
        """
        job_id_path = result.field_jobid or result.field_joblink or result.field_jobtitle
        field_map = {
            "JOBTITLE":   result.field_jobtitle,
            "JOBID":      job_id_path,
            "LOCATION":   result.field_location,
            "EXPERIENCE": result.field_experience,
            "JOBLINK":    result.field_joblink,
            "JOBDESC":    result.field_jobdesc,
        }

        # Filter out empty paths
        mapped = {col: path for col, path in field_map.items() if path}
        if not mapped:
            return {}

        # If a candidate and raw JSON is available, use the robust walker/resolver logic from temp.html
        if candidate and candidate.response_body:
            try:
                import json
                response_json = json.loads(candidate.response_body.strip())
                
                # Dynamic Walker
                all_paths = []
                array_paths = []
                path_order = []
                
                def walk(node, path):
                    if isinstance(node, list):
                        array_paths.append(",".join(path))
                        for item in node:
                            walk(item, path)
                        return
                    if isinstance(node, dict):
                        for k, v in node.items():
                            walk(v, path + [k])
                        return
                    if path:
                        p = ",".join(path)
                        all_paths.append(p)
                        if p not in path_order:
                            path_order.append(p)
                
                walk(response_json, [])
                
                # Make paths unique
                all_paths = list(dict.fromkeys(all_paths))
                array_paths = list(dict.fromkeys(array_paths))
                
                # Resolve paths
                selected = []
                for col, raw_path in mapped.items():
                    # Normalize: replace dot/whitespace/array notations
                    norm = raw_path.replace(".", ",").replace("[]", "").strip()
                    parts = [p.strip() for p in norm.split(",") if p.strip()]
                    norm_path = ",".join(parts)
                    
                    matched_path = None
                    if norm_path in path_order:
                        matched_path = norm_path
                    else:
                        # Fallback: check if any path in path_order ends with the normalized path
                        for p in path_order:
                            if p.endswith("," + norm_path) or p == norm_path:
                                matched_path = p
                                break
                                
                    if not matched_path and parts:
                        # Ultimate fallback: check if the last segment matches
                        last_seg = parts[-1]
                        for p in path_order:
                            if p.split(",")[-1] == last_seg:
                                matched_path = p
                                break
                                
                    if matched_path:
                        selected.append({
                            "semantic": col,
                            "path": matched_path,
                            "index": path_order.index(matched_path)
                        })
                
                if selected:
                    # Sort by original occurrence index in JSON layout
                    selected.sort(key=lambda x: x["index"])
                    
                    array_path = None
                    field_names = []
                    
                    for item in selected:
                        handled = False
                        # Check which array path is the prefix for this field
                        for ap in array_paths:
                            if item["path"].startswith(ap + ",") or item["path"] == ap:
                                array_path = ap
                                if item["path"].startswith(ap + ","):
                                    field_names.append(item["path"][len(ap) + 1:])
                                else:
                                    field_names.append(item["path"])
                                handled = True
                                break
                        if not handled:
                            field_names.append(item["path"])
                            
                    # JPERL expects commas instead of dots in nested field names
                    field_names_comma = [f.replace(".", ",") for f in field_names]
                    
                    # Reverse both lists according to JPERL schema convention
                    locjson = "|X|".join(reversed(field_names_comma)) + (f"|XX|{array_path.replace('.', ',')}" if array_path else "")
                    locjsonseq = ",".join(reversed([x["semantic"] for x in selected]))
                    
                    logger.info("Compiler: successfully walk-resolved LOCJSON: %s, LOCJSONSEQ: %s", locjson, locjsonseq)
                    return {
                        "LOCJSON": locjson,
                        "LOCJSONSEQ": locjsonseq
                    }
            except Exception as e:
                logger.warning("Compiler: walker resolver failed (%s), falling back to string mapping.", e)

        # ── Fallback string/list manipulation logic ───────────────────────────
        # 1. Identify the parent array key (collection_path from result)
        array_key = result.collection_path
        
        # If collection_path was not set, try to deduce it
        if array_key is None:
            for col, path in mapped.items():
                if "[]" in path:
                    parts = path.split(",")
                    for p in parts:
                        if p.endswith("[]"):
                            array_key = p[:-2]
                            break
                    if array_key:
                        break
                        
        if array_key is None:
            parent_candidates = []
            for col, path in mapped.items():
                parts = path.replace(".", ",").split(",")
                if len(parts) > 1:
                    parent_candidates.append(parts[-2])
            if parent_candidates and all(p == parent_candidates[0] for p in parent_candidates):
                array_key = parent_candidates[0]
                
        if array_key is None:
            first_path = list(mapped.values())[0]
            parts = first_path.replace(".", ",").split(",")
            if len(parts) > 1:
                array_key = parts[-2]
            else:
                array_key = "jobs"

        array_key_comma = array_key.replace(".", ",")

        # 4. Extract relative flat property paths and map to column names
        loc_properties = []
        loc_columns = []
        for col, path in mapped.items():
            norm_path = path.replace(".", ",")
            parts = [p.strip() for p in norm_path.split(",") if p.strip()]
            array_parts = [p.strip() for p in array_key_comma.split(",") if p.strip()]
            
            subparts = parts
            if len(parts) > len(array_parts):
                if parts[:len(array_parts)] == array_parts:
                    subparts = parts[len(array_parts):]
            elif len(parts) > 1:
                subparts = parts[-1:]
                
            subpath_flat = ",".join(subparts)
            if not subpath_flat:
                subpath_flat = "id"
                
            loc_properties.append(subpath_flat)
            loc_columns.append(col)
            
        locjson_str = "|X|".join(loc_properties) + f"|XX|{array_key_comma}"
        locjsonseq_str = ",".join(loc_columns)

        return {
            "LOCJSON": locjson_str,
            "LOCJSONSEQ": locjsonseq_str
        }

    # ── Shared helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_postquery(inp: GeneratorInput) -> str:
        """Generate the SQL UPDATE that identifies this config in the DB."""
        safe_name = inp.company_name.replace("'", "\\'")
        return (
            f"update WEB_JOBS set COMPNAME ='{safe_name}',"
            f"compid ='{inp.crawler_id}', jobConsultant = 'n' "
            f"where  SITE = '{inp.site_id}'"
        )

    def _enforce_joblink_extraction_consistency(
        self,
        body: dict,
        result: LLMExtractionResult
    ) -> None:
        """
        Enforce the core JOBLINK invariant for the LLM/JSON path:

            IF JOBLINK template contains {{VARJOBLINK}}
            THEN LOCJSONSEQ MUST contain "JOBLINK"
            AND the source field providing that value must have known provenance.

        Three outcomes:
          1. Already consistent (LOCJSONSEQ has JOBLINK) → no-op.
          2. field_joblink is explicitly set → add it to LOCJSON + LOCJSONSEQ.
          3. field_joblink is None but field_jobid is available and the template
             was derived from the same identifier field → double-map (JOBID=JOBLINK).
             Only allowed when provenance is deterministically established, not merely
             because the values look similar.
          4. Neither field can establish source provenance → fail closed:
             drop JOBLINK and LANDINGJOBLINK, emit a diagnostic.
        """
        joblink_val = body.get("JOBLINK", "")
        if "{{VARJOBLINK}}" not in joblink_val:
            return  # No substitution needed — nothing to enforce

        locjsonseq = body.get("LOCJSONSEQ", "")
        locjson = body.get("LOCJSON", "")

        if "JOBLINK" in locjsonseq:
            return  # Already consistent

        # Determine which source field provides the raw {{VARJOBLINK}} value.
        source_field: Optional[str] = None
        provenance_note = ""

        if result.field_joblink:
            # Explicit link field — deterministic, no ambiguity
            source_field = result.field_joblink
            provenance_note = f"explicit field_joblink='{source_field}'"
        elif result.field_jobid:
            # JOBLINK template was derived from the jobid field (double-mapping).
            # Allowed only when _build_joblink() chose field_jobid as its raw_val source
            # (i.e. field_joblink was None and template was built from jobid path).
            # This is deterministic: we know exactly why JOBID == JOBLINK.
            source_field = result.field_jobid
            provenance_note = f"double-map: field_jobid='{source_field}' also serves as JOBLINK"
        else:
            # Cannot establish source provenance — fail closed.
            logger.warning(
                "Compiler: JOBLINK template '%s' has {{VARJOBLINK}} but no source field "
                "can be established (field_joblink=None, field_jobid=None). "
                "Dropping JOBLINK to prevent producing an invalid config.",
                joblink_val,
            )
            body.pop("JOBLINK", None)
            body.pop("LANDINGJOBLINK", None)
            return

        # Add JOBLINK column to LOCJSON
        # LOCJSON format: "field1|X|field2|XX|array_key"
        array_key = ""
        locjson_fields_part = locjson
        if "|XX|" in locjson:
            locjson_fields_part, array_key = locjson.rsplit("|XX|", 1)

        # Normalize source_field path to comma-separated form (JPERL convention)
        field_path = (
            source_field
            .replace(".", ",")
            .replace("|X|", ",")
            .split("|XX|")[0]
            .strip(",")
        )

        if locjson_fields_part:
            new_locjson_fields = locjson_fields_part + "|X|" + field_path
        else:
            new_locjson_fields = field_path

        body["LOCJSON"] = (
            (new_locjson_fields + "|XX|" + array_key) if array_key else new_locjson_fields
        )

        # Append JOBLINK to LOCJSONSEQ
        body["LOCJSONSEQ"] = (locjsonseq + ",JOBLINK") if locjsonseq else "JOBLINK"

        logger.info(
            "Compiler: added JOBLINK extractor (%s) to LOCJSONSEQ — template='%s'",
            provenance_note, joblink_val,
        )

    def _inject_jd_patterns(
        self,
        body: dict[str, Any],
        jdrgx_patterns: Optional[list[dict[str, str]]] = None,
        jdjson_patterns: Optional[list[dict[str, str]]] = None,
        legacy_jdrgx: Optional[str] = None,
        legacy_jdrgxseq: Optional[str] = None
    ) -> None:
        """
        Symmetrically writes JDJSON1..N/JDJSONSEQ1..N and JDRGX1..N/JDRGXSEQ1..N.
        Maintains backward compatibility with legacy single jdrgx/jdrgxseq fields.
        """
        # 1. Inject JDJSON patterns if available
        if jdjson_patterns:
            for idx, item in enumerate(jdjson_patterns, 1):
                path = item.get("path", "").strip()
                seq = item.get("seq", "JOBDESC").strip()
                if path:
                    body[f"JDJSON{idx}"] = path
                    body[f"JDJSONSEQ{idx}"] = seq or "JOBDESC"

        # 2. Inject JDRGX patterns if available
        if jdrgx_patterns:
            for idx, item in enumerate(jdrgx_patterns, 1):
                pat = item.get("pattern", "").strip()
                seq = item.get("seq", "JOBDESC").strip()
                if pat:
                    body[f"JDRGX{idx}"] = pat
                    body[f"JDRGXSEQ{idx}"] = seq or "JOBDESC"
        elif legacy_jdrgx and not jdjson_patterns and not any(k.startswith("JDRGX") for k in body):
            body["JDRGX1"] = legacy_jdrgx
            body["JDRGXSEQ1"] = legacy_jdrgxseq or "JOBDESC"

    def _enforce_move_to_jd_consistency(self, body: dict[str, Any], site_id: str) -> None:
        """
        Enforce the core MOVE_TO_JD invariant:
          IF MOVE_TO_JD == 1
          THEN at least one verified JDRGX1..N or JDJSON1..N extractor pattern MUST be present.
          Otherwise, raise ValueError to prevent emitting a broken crawler configuration.
        """
        if body.get("MOVE_TO_JD") == 1:
            has_jdrgx = any(
                k.startswith("JDRGX") and not k.startswith("JDRGXSEQ") and str(body[k]).strip()
                for k in body
            )
            has_jdjson = any(
                k.startswith("JDJSON") and not k.startswith("JDJSONSEQ") and str(body[k]).strip()
                for k in body
            )
            if not (has_jdrgx or has_jdjson):
                raise ValueError(
                    f"Invalid JPERL configuration for site '{site_id}': "
                    "MOVE_TO_JD=1 requires at least one verified JDRGX or JDJSON detail extractor."
                )

    @staticmethod
    def _find_nth_capturing_group(pattern: str, target_idx: int) -> tuple[int, int]:
        """
        Find the start and end character indices of the target_idx-th (0-indexed)
        capturing group in pattern. Returns (start_idx, end_idx) or (-1, -1).
        """
        cap_count = -1
        i = 0
        n = len(pattern)
        while i < n:
            if pattern[i] == '\\':
                i += 2  # skip escaped character
                continue
            if pattern[i] == '(':
                # Check if non-capturing or flag/lookaround: (?:, (?=, (?!, (?<=, (?<!, (?s, (?i, etc.
                if i + 1 < n and pattern[i + 1] == '?':
                    pass
                else:
                    cap_count += 1
                    if cap_count == target_idx:
                        depth = 1
                        j = i + 1
                        while j < n:
                            if pattern[j] == '\\':
                                j += 2
                                continue
                            if pattern[j] == '[':
                                # Skip character class [...]
                                j += 1
                                while j < n:
                                    if pattern[j] == '\\':
                                        j += 2
                                        continue
                                    if pattern[j] == ']':
                                        j += 1
                                        break
                                    j += 1
                                continue
                            if pattern[j] == '(':
                                depth += 1
                            elif pattern[j] == ')':
                                depth -= 1
                                if depth == 0:
                                    return (i, j + 1)
                            j += 1
            elif pattern[i] == '[':
                # Skip character class
                i += 1
                while i < n:
                    if pattern[i] == '\\':
                        i += 2
                        continue
                    if pattern[i] == ']':
                        i += 1
                        break
                    i += 1
                continue
            i += 1
        return (-1, -1)

    @classmethod
    def _ensure_locrgx_jobid(cls, locrgx: str, locrgxseq: str) -> tuple[str, str]:
        """
        Ensures JOBID is present in LOCRGX and LOCRGXSEQ for JPERL prod crawler compatibility.
        If missing, applies the fallback hierarchy:
          Explicit JOBID -> JOBLINK -> Job-specific JOBTITLE
        by nesting parentheses around the target capture group: ((...)).
        """
        if not locrgx or not locrgxseq:
            return locrgx, locrgxseq

        fields = [f.strip() for f in locrgxseq.split(",") if f.strip()]
        if "JOBID" in fields:
            return locrgx, locrgxseq

        target_field = None
        if "JOBLINK" in fields:
            target_field = "JOBLINK"
        elif "JOBTITLE" in fields:
            target_field = "JOBTITLE"

        if not target_field:
            return locrgx, locrgxseq

        target_idx = fields.index(target_field)
        s, e = cls._find_nth_capturing_group(locrgx, target_idx)
        if s == -1 or e == -1:
            logger.warning("Compiler: could not find group %d for %s in locrgx '%s'", target_idx, target_field, locrgx)
            return locrgx, locrgxseq

        # Wrap target capture group in outer parentheses ((...))
        new_locrgx = locrgx[:s] + "(" + locrgx[s:e] + ")" + locrgx[e:]
        # Insert JOBID right before the target field
        new_fields = list(fields)
        new_fields.insert(target_idx, "JOBID")
        new_locrgxseq = ",".join(new_fields)

        return new_locrgx, new_locrgxseq


