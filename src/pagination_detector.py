"""
pagination_detector.py
───────────────────────
Detects, classifies, and parameterizes pagination across HTTP requests.

Supports:
  1. GET query parameters (e.g. ?paged=2, ?page=1, ?pageNo=3, ?offset=20, ?startrow=10)
  2. GET URL path segments (e.g. /page/2/, /p/2/)
  3. POST form-urlencoded bodies (e.g. action=loadmore&paged=1&listings_per_page=10)
  4. POST JSON payloads (e.g. {"page": 2, "pageSize": 10}, {"offset": 20})

Transforms numeric pagination parameters into legacy JPERL tokens:
  - Page-number based: '!0o!CURPG!0o!'
  - Offset/row-number based: '!0o!STARTJOBNO!0o!'
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.models import CapturedRequest

logger = logging.getLogger(__name__)

# Known parameter names representing page number
_PAGE_PARAM_NAMES = frozenset({
    "page", "paged", "pageno", "page_no", "pageno_", "currentpage",
    "currpage", "pg", "p", "page_num", "pagenumber", "pageindex",
    "page_index", "page_id", "pgno", "p_p_id",
})

# Known parameter names representing item offset / start row
_OFFSET_PARAM_NAMES = frozenset({
    "offset", "startrow", "start_row", "startindex", "start_index",
    "start", "skip", "from", "rowfrom", "row_from", "begin",
})

# Known parameter names representing page size or item count (never page number)
_PAGE_SIZE_PARAM_NAMES = frozenset({
    "pagesize", "page_size", "per_page", "perpage", "limit", "listings_per_page",
    "items_per_page", "num", "count", "size", "take", "max", "maxresults",
    "max_results", "batch_size", "batchsize",
})


@dataclass
class PaginatedRequestInfo:
    """Detailed metadata and parameterized representations of a paginated request."""
    original_url: str
    original_method: str
    original_body: Optional[str] = None
    original_headers: dict[str, str] = field(default_factory=dict)

    # Classification
    param_name: str = ""
    location: str = "query"            # "query" | "body_form" | "body_json" | "path"
    pagination_type: str = "page"       # "page" | "offset"
    start_value: int = 1
    current_value: int = 1

    # Parameterized outputs with JPERL tokens
    token: str = "!0o!CURPG!0o!"
    parameterized_url: str = ""
    parameterized_body: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)

    def render_request(self, page_num: int, page_size: int = 10) -> tuple[str, Optional[str]]:
        """
        Simulates how JPERL executes the request for a specific page number.
        Returns (rendered_url, rendered_body).
        """
        if self.pagination_type == "offset":
            # Offset calculation: offset = (page_num - 1) * page_size
            offset_val = str((page_num - 1) * page_size)
            rendered_url = self.parameterized_url.replace(self.token, offset_val)
            rendered_body = (
                self.parameterized_body.replace(self.token, offset_val)
                if self.parameterized_body
                else None
            )
        else:
            page_val = str(page_num)
            rendered_url = self.parameterized_url.replace(self.token, page_val)
            rendered_body = (
                self.parameterized_body.replace(self.token, page_val)
                if self.parameterized_body
                else None
            )
        return rendered_url, rendered_body


class PaginationDetector:
    """
    Analyzes network requests and request bodies to detect and parameterize pagination.
    """

    @classmethod
    def detect_and_parametrize(
        cls,
        req: CapturedRequest | dict[str, Any],
    ) -> Optional[PaginatedRequestInfo]:
        """
        Inspects an HTTP request and returns PaginatedRequestInfo if pagination is detected.
        """
        if isinstance(req, dict):
            url = req.get("url", "")
            method = req.get("method", "GET").upper()
            body = req.get("request_body") or req.get("body")
            headers = req.get("request_headers") or req.get("headers") or {}
        else:
            url = req.url
            method = req.method.upper()
            body = req.request_body
            headers = req.request_headers or {}

        # 1. Check POST body (form-urlencoded or JSON)
        if method == "POST" and body:
            # Try form-urlencoded first
            form_info = cls._detect_in_form_body(url, method, body, headers)
            if form_info:
                return form_info

            # Try JSON body
            json_info = cls._detect_in_json_body(url, method, body, headers)
            if json_info:
                return json_info

        # 2. Check URL Query Parameters (both GET and POST can have query params)
        query_info = cls._detect_in_query_params(url, method, body, headers)
        if query_info:
            return query_info

        # 3. Check URL Path Patterns (e.g. /page/2/ or /p/2/)
        path_info = cls._detect_in_url_path(url, method, body, headers)
        if path_info:
            return path_info

        return None

    @classmethod
    def _detect_in_form_body(
        cls, url: str, method: str, body: str, headers: dict[str, str]
    ) -> Optional[PaginatedRequestInfo]:
        """Detects pagination in application/x-www-form-urlencoded body."""
        try:
            pairs = parse_qsl(body, keep_blank_values=True)
            if not pairs:
                return None

            for idx, (k, v) in enumerate(pairs):
                k_lower = k.lower().strip()
                v_clean = v.strip()
                if not v_clean.isdigit():
                    continue

                # Skip parameters representing page sizes, limits, or item counts
                if k_lower in _PAGE_SIZE_PARAM_NAMES or any(k_lower.endswith(s) for s in ("_per_page", "per_page", "pagesize", "_size")):
                    continue

                val_int = int(v_clean)

                if k_lower in _PAGE_PARAM_NAMES or any(k_lower.endswith(p) for p in ("_page", "-page", "paged")):
                    token = "!0o!CURPG!0o!"
                    new_pairs = list(pairs)
                    new_pairs[idx] = (k, token)
                    param_body = "&".join(f"{pk}={pv}" for pk, pv in new_pairs)
                    return PaginatedRequestInfo(
                        original_url=url,
                        original_method=method,
                        original_body=body,
                        original_headers=headers,
                        param_name=k,
                        location="body_form",
                        pagination_type="page",
                        start_value=val_int if val_int in (0, 1) else 1,
                        current_value=val_int,
                        token=token,
                        parameterized_url=url,
                        parameterized_body=param_body,
                        headers=headers,
                    )

                if k_lower in _OFFSET_PARAM_NAMES or any(k_lower.endswith(o) for o in ("_offset", "-offset", "_start")):
                    token = "!0o!STARTJOBNO!0o!"
                    new_pairs = list(pairs)
                    new_pairs[idx] = (k, token)
                    param_body = "&".join(f"{pk}={pv}" for pk, pv in new_pairs)
                    return PaginatedRequestInfo(
                        original_url=url,
                        original_method=method,
                        original_body=body,
                        original_headers=headers,
                        param_name=k,
                        location="body_form",
                        pagination_type="offset",
                        start_value=0,
                        current_value=val_int,
                        token=token,
                        parameterized_url=url,
                        parameterized_body=param_body,
                        headers=headers,
                    )
        except Exception as e:
            logger.debug("PaginationDetector: form body parse error (%s)", e)

        return None

    @classmethod
    def _detect_in_json_body(
        cls, url: str, method: str, body: str, headers: dict[str, str]
    ) -> Optional[PaginatedRequestInfo]:
        """Detects pagination in JSON POST body."""
        try:
            trimmed = body.strip()
            if not (trimmed.startswith("{") or trimmed.startswith("[")):
                return None

            data = json.loads(trimmed)
            if not isinstance(data, dict):
                return None

            for k, v in list(data.items()):
                k_lower = k.lower().strip()
                # Skip page size / limit keys
                if k_lower in _PAGE_SIZE_PARAM_NAMES or any(k_lower.endswith(s) for s in ("_per_page", "per_page", "pagesize", "_size")):
                    continue

                if isinstance(v, (int, float)):
                    val_int = int(v)
                elif isinstance(v, str) and v.isdigit():
                    val_int = int(v)
                else:
                    continue

                if k_lower in _PAGE_PARAM_NAMES or any(k_lower.endswith(p) for p in ("_page", "-page", "paged")):
                    token = "!0o!CURPG!0o!"
                    param_body = re.sub(
                        rf'("{re.escape(k)}"\s*:\s*)"?{val_int}"?',
                        rf'\1{token}',
                        body,
                        count=1,
                    )
                    return PaginatedRequestInfo(
                        original_url=url,
                        original_method=method,
                        original_body=body,
                        original_headers=headers,
                        param_name=k,
                        location="body_json",
                        pagination_type="page",
                        start_value=val_int if val_int in (0, 1) else 1,
                        current_value=val_int,
                        token=token,
                        parameterized_url=url,
                        parameterized_body=param_body,
                        headers=headers,
                    )

                if k_lower in _OFFSET_PARAM_NAMES or any(k_lower.endswith(o) for o in ("_offset", "-offset", "_start")):
                    token = "!0o!STARTJOBNO!0o!"
                    param_body = re.sub(
                        rf'("{re.escape(k)}"\s*:\s*)"?{val_int}"?',
                        rf'\1{token}',
                        body,
                        count=1,
                    )
                    return PaginatedRequestInfo(
                        original_url=url,
                        original_method=method,
                        original_body=body,
                        original_headers=headers,
                        param_name=k,
                        location="body_json",
                        pagination_type="offset",
                        start_value=0,
                        current_value=val_int,
                        token=token,
                        parameterized_url=url,
                        parameterized_body=param_body,
                        headers=headers,
                    )
        except Exception as e:
            logger.debug("PaginationDetector: json body parse error (%s)", e)

        return None

    @classmethod
    def _detect_in_query_params(
        cls, url: str, method: str, body: Optional[str], headers: dict[str, str]
    ) -> Optional[PaginatedRequestInfo]:
        """Detects pagination in URL query string."""
        try:
            parsed = urlparse(url)
            if not parsed.query:
                return None

            qsl = parse_qsl(parsed.query, keep_blank_values=True)
            for idx, (k, v) in enumerate(qsl):
                k_lower = k.lower().strip()
                v_clean = v.strip()
                if not v_clean.isdigit():
                    continue

                # Skip page size / limit keys
                if k_lower in _PAGE_SIZE_PARAM_NAMES or any(k_lower.endswith(s) for s in ("_per_page", "per_page", "pagesize", "_size")):
                    continue

                val_int = int(v_clean)

                if k_lower in _PAGE_PARAM_NAMES or any(k_lower.endswith(p) for p in ("_page", "-page", "paged")):
                    token = "!0o!CURPG!0o!"
                    new_qsl = list(qsl)
                    new_qsl[idx] = (k, token)
                    query_str = "&".join(f"{pk}={pv}" for pk, pv in new_qsl)
                    param_url = urlunparse(parsed._replace(query=query_str))
                    return PaginatedRequestInfo(
                        original_url=url,
                        original_method=method,
                        original_body=body,
                        original_headers=headers,
                        param_name=k,
                        location="query",
                        pagination_type="page",
                        start_value=val_int if val_int in (0, 1) else 1,
                        current_value=val_int,
                        token=token,
                        parameterized_url=param_url,
                        parameterized_body=body,
                        headers=headers,
                    )

                if k_lower in _OFFSET_PARAM_NAMES or any(k_lower.endswith(o) for o in ("_offset", "-offset", "_start")):
                    token = "!0o!STARTJOBNO!0o!"
                    new_qsl = list(qsl)
                    new_qsl[idx] = (k, token)
                    query_str = "&".join(f"{pk}={pv}" for pk, pv in new_qsl)
                    param_url = urlunparse(parsed._replace(query=query_str))
                    return PaginatedRequestInfo(
                        original_url=url,
                        original_method=method,
                        original_body=body,
                        original_headers=headers,
                        param_name=k,
                        location="query",
                        pagination_type="offset",
                        start_value=0,
                        current_value=val_int,
                        token=token,
                        parameterized_url=param_url,
                        parameterized_body=body,
                        headers=headers,
                    )
        except Exception as e:
            logger.debug("PaginationDetector: query parse error (%s)", e)

        return None

    @classmethod
    def _detect_in_url_path(
        cls, url: str, method: str, body: Optional[str], headers: dict[str, str]
    ) -> Optional[PaginatedRequestInfo]:
        """Detects path-based pagination like /page/2/ or /p/2/."""
        path_match = re.search(r'/(?:page|p)/(\d+)/?', url, re.IGNORECASE)
        if path_match:
            val_int = int(path_match.group(1))
            token = "!0o!CURPG!0o!"
            start, end = path_match.span(1)
            param_url = url[:start] + token + url[end:]
            return PaginatedRequestInfo(
                original_url=url,
                original_method=method,
                original_body=body,
                original_headers=headers,
                param_name="path_page",
                location="path",
                pagination_type="page",
                start_value=1,
                current_value=val_int,
                token=token,
                parameterized_url=param_url,
                parameterized_body=body,
                headers=headers,
            )
        return None
