import json
import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)

class JsonParser:
    @staticmethod
    def execute(config: dict, api_response: Optional[str]) -> list[dict]:
        """
        Executes a JPERL LOCJSON/LOCJSONSEQ configuration on a JSON response payload.
        """
        if not api_response:
            logger.warning("JsonParser: api_response is empty or None")
            return []

        locjson = config.get("LOCJSON", "").strip()
        locjsonseq = config.get("LOCJSONSEQ", "").strip()
        if not locjson or not locjsonseq:
            logger.warning("JsonParser: LOCJSON or LOCJSONSEQ is missing in config")
            return []

        data = None
        try:
            data = json.loads(api_response.strip())
        except Exception:
            # Check if JSON is embedded inside HTML (e.g. <script type="application/json">, hidden inputs, JS state)
            data = JsonParser._extract_json_from_html(api_response)

        if data is None:
            logger.error("JsonParser: failed to parse API response or HTML source as JSON")
            return []

        try:
            # 1. Parse LOCJSON and LOCJSONSEQ
            raw_semantics = [s.strip() for s in locjsonseq.split(",") if s.strip()]
            semantics = []
            explicit_mappings = {}
            for s in raw_semantics:
                if "=" in s:
                    k, v = s.split("=", 1)
                    semantics.append(k.strip())
                    explicit_mappings[k.strip()] = v.strip()
                else:
                    semantics.append(s)

            parts = [p.strip() for p in locjson.split("|XX|") if p.strip()]
            
            field_paths = []
            array_path = ""

            if explicit_mappings:
                # Direct JSONPath or dot path format (e.g. $.jobs[*] or jobs) with explicit field mappings
                array_path = locjson.replace("$.", "").replace("[*]", "").strip(".")
                for sem in semantics:
                    field_paths.append(explicit_mappings.get(sem, sem.lower()))
            elif len(parts) >= 3:
                num_fields = len(semantics)
                field_paths.append(parts[0])
                for p in parts[1:num_fields]:
                    field_paths.append(p.split("|X|")[-1])
                
                # Extract array path from the second element prefix
                first_rel = parts[1]
                array_segs = first_rel.split("|X|")[:-1]
                array_path = ".".join(array_segs)
            elif len(parts) == 2:
                # Standard JPERL format: field1|X|field2|XX|array_path
                field_part, array_path = parts[0], parts[1]
                field_paths = [p.strip() for p in field_part.split("|X|")]
            else:
                # Fallback for simple single-field or basic path formats (or root array)
                if parts and "|X|" in parts[0]:
                    field_paths = [p.strip() for p in parts[0].split("|X|")]
                    array_path = ""
                else:
                    array_path = locjson.replace("$.", "").replace("[*]", "").strip(".")
                    field_paths = [parts[0]] if parts else []

            # 2. Extract Jobs Array
            jobs_array = JsonParser._get_json_value(data, array_path) if array_path else data
            if not isinstance(jobs_array, list):
                # If it's a dict containing the array, or a single dict
                if isinstance(jobs_array, dict):
                    # Try to find any list in the dict
                    lists = [v for v in jobs_array.values() if isinstance(v, list)]
                    if lists:
                        jobs_array = lists[0]
                    else:
                        jobs_array = [jobs_array]
                else:
                    jobs_array = []

            # 3. Extract Fields for each item
            job_objects = []
            for item in jobs_array:
                job = {}
                for i, field_rel_path in enumerate(field_paths):
                    if i < len(semantics):
                        val = JsonParser._get_json_value(item, field_rel_path)
                        job[semantics[i]] = str(val).strip() if val is not None else ""

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

                if "JOBTITLE" not in job and "JOBID" in job:
                    job["JOBTITLE"] = job["JOBID"]

                job_objects.append(job)

            return job_objects
        except Exception as e:
            logger.error("JsonParser: error executing JSON extraction: %s", e)
            return []

    @staticmethod
    def _extract_json_from_html(html_text: str) -> Optional[Any]:
        import re, html
        # 1. Look for application/json or application/ld+json script tags
        matches = re.findall(r'<script[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>', html_text, re.DOTALL | re.IGNORECASE)
        for m in matches:
            try:
                parsed = json.loads(m.strip())
                if parsed:
                    return parsed
            except Exception:
                pass

        # 2. Look for hidden inputs or data attributes containing escaped/raw JSON
        input_matches = re.findall(r'<input[^>]+value=["\'](\[\{.+?\}\]|(?:&#34;|&quot;|\{.+?\}))["\']', html_text, re.DOTALL | re.IGNORECASE)
        for m in input_matches:
            try:
                unescaped = html.unescape(m)
                parsed = json.loads(unescaped)
                if parsed:
                    return parsed
            except Exception:
                pass

        # 3. Look for JS variables var jobs = [...]; or window.__INITIAL_STATE__ = {...}
        js_matches = re.findall(r'(?:var|let|const|window\.__\w+__)\s*=\s*([\[\{].+?[\]\}]);', html_text, re.DOTALL)
        for m in js_matches:
            try:
                parsed = json.loads(m.strip())
                if parsed:
                    return parsed
            except Exception:
                pass
        return None

    @staticmethod
    def _get_json_value(data: Any, path_str: str) -> Any:
        """
        Traverses a nested dict/list using a dot-normalized or comma-separated path.
        """
        norm = path_str.replace(",", ".").replace("[", ".").replace("]", "").replace("|", ".")
        segments = [s.strip() for s in norm.split(".") if s.strip()]
        
        current = data
        for seg in segments:
            if isinstance(current, dict):
                if seg in current:
                    current = current[seg]
                else:
                    return None
            elif isinstance(current, list):
                try:
                    if seg.isdigit():
                        idx = int(seg)
                        if 0 <= idx < len(current):
                            current = current[idx]
                        else:
                            return None
                    else:
                        return None
                except Exception:
                    return None
            else:
                return None
        return current

    @staticmethod
    def execute_jd(config: dict, detail_json: str | dict) -> dict[str, str]:
        """
        Executes JDJSON1..N and JDJSONSEQ1..N on a detail JSON response.
        Returns a dictionary of extracted detail fields: e.g. {"JOBDESC": "...", "Date": "..."}
        """
        if not detail_json:
            return {}

        if isinstance(detail_json, str):
            try:
                data = json.loads(detail_json.strip())
            except Exception as e:
                logger.debug("JsonParser.execute_jd: failed to parse detail JSON: %s", e)
                return {}
        else:
            data = detail_json

        extracted_fields: dict[str, str] = {}

        # Look for JDJSON1..N and JDJSONSEQ1..N (or JDJSON/JDJSONSEQ)
        idx = 1
        while True:
            key_json = f"JDJSON{idx}" if f"JDJSON{idx}" in config else ("JDJSON" if idx == 1 and "JDJSON" in config else None)
            key_seq = f"JDJSONSEQ{idx}" if f"JDJSONSEQ{idx}" in config else ("JDJSONSEQ" if idx == 1 and "JDJSONSEQ" in config else None)

            if not key_json or not key_seq:
                if idx == 1 and ("JDJSON1" not in config and "JDJSON" not in config):
                    # Check if any JDJSON keys exist
                    any_jd = [k for k in config if k.startswith("JDJSON") and not k.startswith("JDJSONSEQ")]
                    if not any_jd:
                        break
                    idx += 1
                    continue
                else:
                    break

            jdjson_val = str(config.get(key_json, "")).strip()
            jdjsonseq_val = str(config.get(key_seq, "")).strip()

            if jdjson_val and jdjsonseq_val:
                semantics = [s.strip() for s in jdjsonseq_val.split(",") if s.strip()]
                parts = [p.strip() for p in jdjson_val.split("|XX|") if p.strip()]

                field_paths = []
                array_path = ""

                if len(parts) == 2:
                    field_part, array_path = parts[0], parts[1]
                    field_paths = [p.strip() for p in field_part.split("|X|") if p.strip()]
                else:
                    field_paths = [p.strip() for p in parts[0].split("|X|") if p.strip()]

                target_node = JsonParser._get_json_value(data, array_path) if array_path else data
                if isinstance(target_node, list) and target_node:
                    target_node = target_node[0]

                if isinstance(target_node, dict):
                    for i, f_path in enumerate(field_paths):
                        sem = semantics[i] if i < len(semantics) else (semantics[-1] if semantics else "JOBDESC")
                        val = JsonParser._get_json_value(target_node, f_path)
                        if val is not None:
                            val_str = str(val).strip()
                            if val_str:
                                if sem in extracted_fields:
                                    extracted_fields[sem] += "\n\n" + val_str
                                else:
                                    extracted_fields[sem] = val_str

            idx += 1

        return extracted_fields

