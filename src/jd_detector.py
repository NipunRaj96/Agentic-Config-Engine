"""
jd_detector.py
──────────────
Reusable Job Description (JD) region detection, payload classification,
adaptive HTML context windowing, and semantic validation layer.

Decouples signal knowledge (from knowledge_base/jd_signals.json)
from pipeline execution logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_SIGNALS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "knowledge_base",
    "jd_signals.json",
)


@dataclass
class JDSignalWeights:
    structural_container: float = 150.0
    semantic_attribute: float = 120.0
    start_signal_phrase: float = 80.0
    end_signal_phrase: float = 60.0
    noise_container_penalty: float = -150.0
    text_density_bonus: float = 50.0


class JDDetector:
    """
    Centralized detector for detail payload classification,
    adaptive HTML context windowing, and JD text validation.
    """

    _instance: Optional[JDDetector] = None

    def __init__(self, config_path: str = _DEFAULT_SIGNALS_PATH) -> None:
        self.weights = JDSignalWeights()
        self.container_signals: list[str] = []
        self.semantic_attributes: list[str] = []
        self.start_signals: list[str] = []
        self.end_signals: list[str] = []
        self.noise_containers: list[str] = []
        self.json_field_signals: list[str] = []
        self.load_signals(config_path)

    @classmethod
    def get_instance(cls) -> JDDetector:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def load_signals(self, config_path: str) -> None:
        """Load external signals configuration from JSON file."""
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                w = data.get("weights", {})
                self.weights = JDSignalWeights(
                    structural_container=float(w.get("structural_container", 150.0)),
                    semantic_attribute=float(w.get("semantic_attribute", 120.0)),
                    start_signal_phrase=float(w.get("start_signal_phrase", 80.0)),
                    end_signal_phrase=float(w.get("end_signal_phrase", 60.0)),
                    noise_container_penalty=float(w.get("noise_container_penalty", -150.0)),
                    text_density_bonus=float(w.get("text_density_bonus", 50.0)),
                )
                self.container_signals = data.get("JD_CONTAINER_SIGNALS", [])
                self.semantic_attributes = data.get("SEMANTIC_ATTRIBUTES", [])
                self.start_signals = data.get("JD_START_SIGNALS", [])
                self.end_signals = data.get("JD_END_SIGNALS", [])
                self.noise_containers = data.get("JD_NOISE_CONTAINERS", [])
                self.json_field_signals = data.get("JD_JSON_FIELD_SIGNALS", [])
                logger.debug("JDDetector: successfully loaded signals from %s", config_path)
                return
        except Exception as exc:
            logger.warning("JDDetector: failed to load signals from %s (%s), using fallbacks", config_path, exc)

        # Fallback defaults if file loading fails
        self.container_signals = [
            "job-description", "job_description", "jobDescription", "job-desc", "job_desc",
            "job-details", "job_details", "job-content", "job-post", "posting-description"
        ]
        self.semantic_attributes = [
            "itemprop=\"description\"", "id=\"job-description\"", "data-automation=\"job-description\""
        ]
        self.start_signals = [
            "job description", "about the role", "responsibilities", "what you'll do",
            "requirements", "qualifications", "skills required", "experience required"
        ]
        self.end_signals = [
            "apply now", "apply for this job", "submit application", "share this job",
            "similar jobs", "related jobs", "back to jobs", "how to apply"
        ]
        self.noise_containers = ["header", "footer", "nav", "sidebar", "menu", "cookie"]
        self.json_field_signals = ["description", "jobDescription", "longDescription", "details", "body"]

    # ── 1. Detail Payload Classification ──────────────────────────────────────

    @staticmethod
    def detect_payload_type(response_text: str, content_type: Optional[str] = None) -> str:
        """
        Classifies detail response payload into:
          - 'JSON': Direct JSON API response
          - 'HTML_WITH_EMBEDDED_JSON': HTML containing <script type='application/ld+json'> or __NEXT_DATA__
          - 'HTML': Server-rendered HTML page
          - 'EMPTY': Empty / blank response
        """
        if not response_text or not response_text.strip():
            return "EMPTY"

        text_strip = response_text.strip()
        ct_lower = (content_type or "").lower()

        # 1. Direct JSON check
        if "application/json" in ct_lower or "application/problem+json" in ct_lower:
            try:
                json.loads(text_strip)
                return "JSON"
            except Exception:
                pass

        if text_strip.startswith(("{", "[")) and text_strip.endswith(("}", "]")):
            try:
                json.loads(text_strip)
                return "JSON"
            except Exception:
                pass

        # 2. HTML check
        if "<script" in text_strip.lower():
            if 'type="application/ld+json"' in text_strip or "type='application/ld+json'" in text_strip:
                return "HTML_WITH_EMBEDDED_JSON"
            if '__NEXT_DATA__' in text_strip or 'window.__INITIAL_STATE__' in text_strip:
                return "HTML_WITH_EMBEDDED_JSON"

        return "HTML"

    # ── 2. Boilerplate Stripping & Adaptive Windowing ──────────────────────────

    def distill_and_window_json(self, raw_json: str, max_chars: int = 35_000) -> tuple[str, dict[str, Any]]:
        """
        Structured JSON subtree extraction:
          - Parses JSON structure
          - Identifies candidate JD subtrees using JD_JSON_FIELD_SIGNALS and keyword density
          - Prunes large unrelated arrays / metadata while preserving the exact path hierarchy
          - Returns context JSON string for LLM prompting (<= max_chars)
        """
        if not raw_json or not raw_json.strip():
            return "{}", {"strategy": "empty"}

        try:
            data = json.loads(raw_json.strip())
        except Exception:
            # Fallback if invalid JSON: truncate safely
            return raw_json[:max_chars], {"strategy": "raw_truncation"}

        if len(raw_json) <= max_chars:
            return raw_json, {"strategy": "full_json", "original_size": len(raw_json)}

        signal_keys = set(k.lower() for k in self.json_field_signals)

        def _is_jd_candidate_key(k: str) -> bool:
            k_clean = re.sub(r'[^a-zA-Z0-9]', '', str(k).lower())
            for sig in signal_keys:
                sig_clean = re.sub(r'[^a-zA-Z0-9]', '', sig.lower())
                if sig_clean and (sig_clean in k_clean or k_clean in sig_clean):
                    return True
            return False

        def _prune(node: Any, depth: int = 0) -> Any:
            if depth > 20:
                return "... (max depth reached)"

            if isinstance(node, dict):
                pruned_dict = {}
                for k, v in node.items():
                    is_candidate = _is_jd_candidate_key(k)
                    if isinstance(v, (dict, list)):
                        pruned_dict[k] = _prune(v, depth + 1)
                    elif isinstance(v, str):
                        # Preserve full JD text if candidate or long descriptive text
                        if is_candidate or (len(v) >= 100 and any(kw in v.lower() for kw in self.start_signals[:10])):
                            pruned_dict[k] = v
                        elif len(v) > 300:
                            pruned_dict[k] = v[:150] + f"... [truncated {len(v)} chars]"
                        else:
                            pruned_dict[k] = v
                    else:
                        pruned_dict[k] = v
                return pruned_dict

            elif isinstance(node, list):
                if not node:
                    return []
                # If array has many items, preserve at most 2 items and summarize remainder
                if len(node) > 2:
                    pruned_list = [_prune(item, depth + 1) for item in node[:2]]
                    pruned_list.append(f"... [{len(node) - 2} items omitted]")
                    return pruned_list
                return [_prune(item, depth + 1) for item in node]

            return node

        pruned_data = _prune(data)
        serialized = json.dumps(pruned_data, indent=2)
        if len(serialized) > max_chars:
            serialized = serialized[:max_chars]

        return serialized, {
            "strategy": "structured_subtree_pruning",
            "original_size": len(raw_json),
            "windowed_size": len(serialized)
        }

    def distill_html(self, raw_html: str) -> str:
        """
        Strips non-semantic boilerplate: style, non-ld-json script, svg, header, footer, nav.
        """
        if not raw_html:
            return ""

        import html
        cleaned = html.unescape(raw_html)
        cleaned = re.sub(r'(?s)<!--.*?-->', '', cleaned)
        cleaned = re.sub(r'(?s)<style\b[^>]*>.*?</style>', '', cleaned)
        cleaned = re.sub(r'(?s)<svg\b[^>]*>.*?</svg>', '', cleaned)
        cleaned = re.sub(r'(?s)<header\b[^>]*>.*?</header>', '', cleaned)
        cleaned = re.sub(r'(?s)<footer\b[^>]*>.*?</footer[^>]*>', '', cleaned)
        cleaned = re.sub(r'(?s)<nav\b[^>]*>.*?</nav>', '', cleaned)

        # Strip standard scripts but preserve scripts containing job description data or state objects
        def _script_filter(match: re.Match) -> str:
            tag_open = match.group(1) or ""
            script_body = match.group(2) or ""
            if "application/ld+json" in tag_open.lower():
                return match.group(0)
            if any(sig.lower() in script_body.lower() for sig in [
                "job_description", "jobdescription", "posting_title",
                "__next_data__", "__initial_state__", "modulerecordmapping", "job_details"
            ]):
                return match.group(0)
            return ""

        cleaned = re.sub(r'(?s)(<script\b[^>]*>)(.*?)</script>', _script_filter, cleaned)
        cleaned = re.sub(r'data:[^;]+;base64,[A-Za-z0-9+/=\s]+', '', cleaned)
        return cleaned

    def distill_and_window_html(
        self,
        raw_html: str,
        min_chars: int = 15_000,
        max_chars: int = 50_000
    ) -> Tuple[str, dict[str, Any]]:
        """
        Produces an adaptive, token-efficient HTML context window centered
        around the most prominent JD container and anchors.
        
        Preserves opening tags before the start anchor and closing tags after the end anchor.
        """
        cleaned = self.distill_html(raw_html)
        if len(cleaned) <= min_chars:
            return cleaned, {"start_idx": 0, "end_idx": len(cleaned), "strategy": "full_cleaned"}

        cleaned_lower = cleaned.lower()

        # Step 1: Detect highest-scoring container / semantic attribute
        best_container_idx = None
        best_container_score = -1.0

        for attr in self.semantic_attributes:
            idx = cleaned_lower.find(attr.lower())
            if idx != -1:
                score = self.weights.semantic_attribute
                if score > best_container_score:
                    best_container_score = score
                    best_container_idx = idx

        for container in self.container_signals:
            pat = rf'(?i)(?:class|id)=["\'][^"\']*?\b{re.escape(container)}\b'
            for m in re.finditer(pat, cleaned):
                idx = m.start()
                score = self.weights.structural_container
                if score > best_container_score:
                    best_container_score = score
                    best_container_idx = idx

        # Step 2: Detect first strong JD Start signal
        best_start_idx = None
        for signal in self.start_signals:
            pat = rf'(?i)\b{re.escape(signal)}\b'
            m = re.search(pat, cleaned)
            if m:
                idx = m.start()
                if best_start_idx is None or idx < best_start_idx:
                    best_start_idx = idx

        # Determine reference start point
        ref_start = None
        if best_container_idx is not None and best_start_idx is not None:
            # If container appears shortly before start signal, pick container
            if best_container_idx <= best_start_idx or (best_container_idx - best_start_idx < 1000):
                ref_start = min(best_container_idx, best_start_idx)
            else:
                ref_start = best_start_idx
        elif best_container_idx is not None:
            ref_start = best_container_idx
        elif best_start_idx is not None:
            ref_start = best_start_idx
        else:
            # Fallback to general career text
            m = re.search(r'(?i)\b(?:job|career|role|position|opening|vacancy)\b', cleaned)
            ref_start = m.start() if m else 0

        # Step 3: Detect best JD End signal AFTER ref_start
        search_start = max(0, ref_start + 200)
        best_end_idx = None
        search_zone = cleaned[search_start:]

        for signal in self.end_signals:
            pat = rf'(?i)\b{re.escape(signal)}\b'
            m = re.search(pat, search_zone)
            if m:
                end_pos = search_start + m.end()
                # Prefer end signal that is at least 300 chars after start
                if end_pos - ref_start >= 300:
                    if best_end_idx is None or end_pos < best_end_idx:
                        best_end_idx = end_pos

        # Step 4: Construct adaptive window
        # Ensure opening tags before ref_start are preserved (e.g. 1500 chars before)
        win_start = max(0, ref_start - 1500)

        if best_end_idx is not None:
            # Ensure closing tags after best_end_idx are preserved (e.g. 1500 chars after)
            win_end = min(len(cleaned), best_end_idx + 1500)
            span = win_end - win_start

            # If span is too small, expand symmetrically up to min_chars
            if span < min_chars:
                deficit = min_chars - span
                win_start = max(0, win_start - deficit // 2)
                win_end = min(len(cleaned), win_end + deficit // 2)

            # If span exceeds max_chars, recenter around start
            if (win_end - win_start) > max_chars:
                win_end = min(len(cleaned), win_start + max_chars)

            strategy = "start_and_end_anchors"
        else:
            # No clear end anchor found inside standard span -> expand adaptively
            win_end = min(len(cleaned), win_start + min(max_chars, 32_000))
            strategy = "start_anchor_adaptive_span"

        windowed = cleaned[win_start:win_end]
        meta = {
            "start_idx": win_start,
            "end_idx": win_end,
            "total_len": len(cleaned),
            "ref_start": ref_start,
            "ref_end": best_end_idx,
            "strategy": strategy
        }
        return windowed, meta

    # ── 3. Semantic Job Description Validation ─────────────────────────────────

    def validate_jd_text(self, text: Optional[str], raw_full_text: Optional[str] = None) -> Tuple[bool, str]:
        """
        Validates extracted job description text:
          - Non-empty string
          - Length between 150 and 80,000 chars
          - Contains descriptive phrases / keywords
          - Not pure navigation / privacy noise
          - Not identical to the whole raw HTML/JSON page
        """
        if text is None:
            return False, "Extracted JD text is None."

        strip_text = text.strip()
        if not strip_text:
            return False, "Extracted JD text is empty."

        clean_text = re.sub(r'<[^>]+>', ' ', strip_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        text_len = len(clean_text)

        if text_len < 100:
            return False, f"Extracted JD text is too short ({text_len} chars, minimum is 100)."

        if text_len > 80_000:
            return False, f"Extracted JD text is excessively long ({text_len} chars), likely captured entire page body."

        # Check if identical to raw full page
        if raw_full_text and len(raw_full_text) > 0:
            if text_len >= len(raw_full_text) * 0.90:
                return False, "Extracted JD text spans >90% of entire raw page. Regex/JSON selector is too broad."

        # Semantic keywords check
        lower_text = clean_text.lower()
        keywords = [
            "responsibilit", "qualificat", "requirement", "experience", "skills",
            "role", "duties", "benefits", "about", "team", "project", "work",
            "candidate", "developer", "engineer", "manager", "analyst", "design"
        ]
        matched_kw = sum(1 for kw in keywords if kw in lower_text)
        if matched_kw < 1 and text_len < 500:
            return False, "Extracted text does not contain any job description terminology."

        # Noise phrases check
        noise_phrases = [
            "privacy policy", "all rights reserved", "terms of use", "cookie policy",
            "contact us", "subscribe to newsletter"
        ]
        matched_noise = sum(1 for np in noise_phrases if np in lower_text)
        if matched_noise >= 3 and text_len < 600:
            return False, "Extracted text appears to be footer / privacy noise rather than job description."

        return True, ""
