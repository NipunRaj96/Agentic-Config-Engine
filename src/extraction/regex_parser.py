import re
import logging
import concurrent.futures
from typing import Optional

logger = logging.getLogger(__name__)

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def safe_regex_findall(compiled: re.Pattern, text: str, timeout_s: float = 2.0) -> list:
    """Executes compiled.findall(text) with a strict execution timeout to prevent ReDoS hangs."""
    future = _EXECUTOR.submit(compiled.findall, text)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        logger.warning("RegexParser: regex execution timed out (>%.1fs) due to potential ReDoS/backtracking", timeout_s)
        raise TimeoutError(f"Regex execution timed out (>{timeout_s}s)")


def safe_regex_search(compiled: re.Pattern, text: str, timeout_s: float = 2.0) -> Optional[re.Match]:
    """Executes compiled.search(text) with a strict execution timeout to prevent ReDoS hangs."""
    future = _EXECUTOR.submit(compiled.search, text)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        logger.warning("RegexParser: regex search timed out (>%.1fs) due to potential ReDoS/backtracking", timeout_s)
        return None


class RegexParser:
    @staticmethod
    def execute(config: dict, page_html: Optional[str]) -> list[dict]:
        """
        Executes a JPERL LOCRGX pattern on HTML and extracts unified job objects.
        """
        if not page_html:
            logger.warning("RegexParser: page_html is empty or None")
            return []

        locrgx = config.get("LOCRGX", "").strip()
        locrgxseq = config.get("LOCRGXSEQ", "").strip()
        if not locrgx or not locrgxseq:
            logger.warning("RegexParser: LOCRGX or LOCRGXSEQ is missing in config")
            return []

        try:
            # Strip comments to avoid commented-out matches
            html_clean = re.sub(r'(?s)<!--.*?-->', '', page_html)
            
            compiled = re.compile(locrgx, re.DOTALL)
            raw_matches = safe_regex_findall(compiled, html_clean, timeout_s=2.0)
            if not raw_matches:
                return []

            fields = [f.strip() for f in locrgxseq.split(",")]
            job_objects = []

            for m in raw_matches:
                job = {}
                if isinstance(m, tuple):
                    for i, val in enumerate(m):
                        if i < len(fields):
                            job[fields[i]] = val.strip() if val is not None else ""
                else:
                    if fields:
                        job[fields[0]] = m.strip() if m is not None else ""

                # Deterministic JOBID hierarchy: Explicit JOBID -> JOBLINK -> Job-specific JOBTITLE
                job_id = job.get("JOBID", "").strip()
                if not job_id:
                    if job.get("JOBLINK", "").strip():
                        job["JOBID"] = job["JOBLINK"].strip()
                    elif job.get("JOBTITLE", "").strip():
                        job["JOBID"] = job["JOBTITLE"].strip()
                    else:
                        job["JOBID"] = ""
                else:
                    job["JOBID"] = job_id

                if "JOBLINK" not in job:
                    job["JOBLINK"] = ""
                if "JOBTITLE" not in job and "JOBID" in job:
                    job["JOBTITLE"] = job["JOBID"]

                job_objects.append(job)

            return job_objects
        except Exception as e:
            logger.error("RegexParser: failed to execute regex extraction: %s", e)
            return []

    @staticmethod
    def execute_jd(config: dict, detail_html: Optional[str]) -> dict[str, str]:
        """
        Executes JDRGX1..N and JDRGXSEQ1..N on a detail HTML response.
        Returns a dictionary of extracted detail fields: e.g. {"JOBDESC": "...", "Date": "..."}
        """
        if not detail_html:
            return {}

        import html
        html_clean = html.unescape(detail_html)
        html_clean = re.sub(r'(?s)<!--.*?-->', '', html_clean)

        extracted_fields: dict[str, str] = {}

        idx = 1
        while True:
            key_rgx = f"JDRGX{idx}" if f"JDRGX{idx}" in config else ("JDRGX" if idx == 1 and "JDRGX" in config else None)
            key_seq = f"JDRGXSEQ{idx}" if f"JDRGXSEQ{idx}" in config else ("JDRGXSEQ" if idx == 1 and "JDRGXSEQ" in config else None)

            if not key_rgx or not key_seq:
                if idx == 1 and ("JDRGX1" not in config and "JDRGX" not in config):
                    any_jd = [k for k in config if k.startswith("JDRGX") and not k.startswith("JDRGXSEQ")]
                    if not any_jd:
                        break
                    idx += 1
                    continue
                else:
                    break

            jdrgx_val = str(config.get(key_rgx, "")).strip()
            jdrgxseq_val = str(config.get(key_seq, "")).strip()

            if jdrgx_val and jdrgxseq_val:
                semantics = [s.strip() for s in jdrgxseq_val.split(",") if s.strip()]
                try:
                    compiled = re.compile(jdrgx_val, re.DOTALL)
                    m = safe_regex_search(compiled, html_clean, timeout_s=2.0)
                    if m:
                        if m.groups():
                            for g_idx, g_val in enumerate(m.groups()):
                                sem = semantics[g_idx] if g_idx < len(semantics) else (semantics[-1] if semantics else "JOBDESC")
                                if g_val is not None:
                                    val_str = str(g_val).strip()
                                    if val_str:
                                        if sem in extracted_fields:
                                            extracted_fields[sem] += "\n\n" + val_str
                                        else:
                                            extracted_fields[sem] = val_str
                        else:
                            sem = semantics[0] if semantics else "JOBDESC"
                            val_str = m.group(0).strip()
                            if val_str:
                                extracted_fields[sem] = val_str
                except Exception as exc:
                    logger.debug("RegexParser.execute_jd: pattern error for %s: %s", key_rgx, exc)

            idx += 1

        return extracted_fields

