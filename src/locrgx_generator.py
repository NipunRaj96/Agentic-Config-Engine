"""
locrgx_generator.py
────────────────────
Pipeline step: Generate LOCRGX (HTML regex) config from the rendered
career page HTML or an HTML-returning AJAX endpoint.

Fires: ALWAYS for non-ATS sites (any site where detection_path is not 'ats').
       Skips if no HTML data is available (page_html and html_candidates both empty).

Design:
  - HTML source selection is DETERMINISTIC (no LLM) — scored by job keyword density
  - LLM is called only for regex generation (few-shot, structured output)
  - Regex is VALIDATED against the HTML before accepting (accuracy > latency)
  - If MOVE_TO_JD=1: fetches first job link to capture JD HTML, generates JDRGX
  - Structured tech_comments on every HALT_FAIL (step / signal / reason / TechOps ask)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from src.jd_extractor import JDExtractor
from src.llm_client import LLMClient
from src.pagination_detector import PaginationDetector
from src.models import (
    CrawlerType,
    GeneratorInput,
    HTMLCandidate,
    LOCRGXResult,
    SiteType,
    SubTechComment,
    TechStatus,
    SourceType,
)
from src.pipeline_step import PipelineState, PipelineStep, StepResult, StepSignal

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────────

_CONF_THRESHOLD   = 0.35   # minimum LLM confidence to accept a regex
_MAX_HTML_CHARS   = 30_000  # truncate HTML sent to LLM (token budget)
_JD_FETCH_TIMEOUT = 8       # seconds for JD page fetch

# Known LOCRGXSEQ field names TechOps uses
_VALID_SEQ_FIELDS = frozenset({
    "JOBTITLE", "JOBID", "JOBLINK", "LOCATION", "EXPERIENCE",
    "Date", "JOBDESC", "SALARY", "EDUCATION",
})

_ZOHO_INSTRUCTION = r"""

*** CRITICAL INSTRUCTION FOR ZOHO RECRUIT SITES ***
This is a Zoho Recruit hosted careers site. Zoho Recruit sites serialize job data inside a hidden input tag or JSON variable (e.g. `<input type="hidden" value="[{&#34;Posting_Title&#34;:&#34;...&#34;...}]">`) in the raw static HTML response.
1. Look for the `value="[{&#34;Posting_Title&#34;...}]"` hidden input or JSON array containing job objects.
2. Notice that the HTML contains HTML entity encodings like `&#34;` or `&quot;` for quotes.
3. In Zoho Recruit, each job opening contains structured keys:
   - `Posting_Title` (or `Job_Opening_Name`): Map to `JOBTITLE`
   - `City` (or `State`/`Country`): Map to `LOCATION` if non-empty
   - `Work_Experience`: Map to `EXPERIENCE` if non-empty
   - `Date_Opened`: Map to `Date` if non-empty
   - `id`: The unique detail link identifier (/jobs/Careers/{id}). ALWAYS map to `JOBLINK`!
4. Write a robust PCRE regex matching all structured keys present in the job objects:
   - Example when City, Work_Experience, Date_Opened, and id are present:
     `(?s)(?=(?:&#34;|&quot;)Posting_Title(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)City(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Work_Experience(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)id(?:&#34;|&quot;):\s*(?:&#34;|&quot;)(\d+)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Date_Opened(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;))(?:&#34;|&quot;)Posting_Title(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)City(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Work_Experience(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)id(?:&#34;|&quot;):\s*(?:&#34;|&quot;)(\d+)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Date_Opened(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)`
     LOCRGXSEQ: `JOBTITLE,LOCATION,EXPERIENCE,JOBLINK,Date`
   - If `City` or `Work_Experience` is omitted or null in the JSON object (e.g. Shift Boolean), omit them from the capture sequence and capture `JOBTITLE,JOBLINK`.
5. Set `move_to_jd=1` so the system automatically navigates to the job detail page (`/jobs/Careers/{{VARJOBLINK}}`) and extracts the full job description into JDRGX.
"""

_SAPIZON_INSTRUCTION = """

*** CRITICAL INSTRUCTION FOR SAPIZON TECHNOLOGIES LLP ***
This website renders job details in sequential `<p>` tags containing `<strong>` labels with variable whitespace (e.g. `<p><strong>Position:</strong>`, `<p> <strong>Location: </strong>`, `<p> <strong> Job Description:</strong>`).
To match this structure robustly:
1. Start the pattern matching the Position label. Use flexible tag and whitespace matching: `Position[^>]*>\\s*([^<]+)`
2. Use `[^>]+>` and `[^<]+` to step through the other fields like Experience, Education, and Location. Do not hardcode literal tags or tags with strict spacing.
3. For example, a robust JPERL regex pattern that works is:
`(?s)Position[^>]+>([^<]+)[^>]+>[^>]+>[^>]+>[^>]+>([^<]+)[^>]+>[^>]+>[^>]+>[^>]+>([^<]+)[^>]+>[^>]+>[^>]+>[^>]+>([^<]+)[^>]+>(.+?)[\\"']Apply`
Make sure to output a pattern like this, with `move_to_jd=0` since the description is fully visible on the same page.
"""

_WEBCOOKS_INSTRUCTION = """

*** CRITICAL INSTRUCTION FOR WEBCOOKS TECHNOLOGIES ***
This careers site displays job listings as static cards without any anchor `<a>` links or `href` attributes (there are no job detail pages).
1. Do NOT try to find a real URL/href for JOBLINK. Set `move_to_jd=0`.
2. Define your LOCRGX pattern to match the repeating job card structures (look for container divs containing `<h3>` for job title and metadata inside other tags). Do NOT match the `<select name="designation">` element which is just a dropdown selector.
3. Since there are no links, only capture JOBTITLE.
4. A robust JPERL regex pattern that works is:
`(?s)<div[^>]*class="group p-6 rounded-3xl bg-white[^"]*">.*?<h3[^>]*>([^<]+)</h3>`
Ensure LOCRGXSEQ is set exactly to: `JOBTITLE`
"""

_ISI_INSTRUCTION = """

*** CRITICAL INSTRUCTION FOR ISI INDIA (ISI SECURITY) ***
This careers site displays job listings as flex cards inside a list. These cards do NOT contain anchor `<a>` href links because job details open in modals dynamically via `<button>` clicks.
1. Do NOT try to find a real URL/href for JOBLINK. Set `move_to_jd=0`.
2. Define your LOCRGX pattern to match the repeating job card containers (look for divs with classes like `group relative bg-card` or container structures).
3. Note that the job titles in the heading `<h3>` tags might contain special unicode spaces or hyphens (e.g. em-dashes). Use flexible patterns like `([^<]+)` inside headings rather than matching literal strings.
4. Since there are no links, only capture JOBTITLE and LOCATION.
5. A robust JPERL regex pattern that works is:
`(?s)<div[^>]*class="group relative bg-card[^"]*">.*?<h3[^>]*>([^<]+)</h3>.*?class="font-medium leading-snug"[^>]*>([^<]+)</span>`
Ensure LOCRGXSEQ is set exactly to: `JOBTITLE,LOCATION`
"""

_JS_INSTRUCTION = """

*** CRITICAL INSTRUCTION FOR JAVASCRIPT ASSET FILES ***
The source content is a raw JavaScript bundle/file containing a hardcoded array of job objects, NOT HTML tags.
1. Define your LOCRGX pattern to match the javascript object literals, e.g. using patterns like `(?s)\\{id:\\s*[\"']?([^\"',]+)[\"']?,\\s*title:\\s*[\"']([^\"']+)[\"']` or similar depending on the exact format in the snippet.
2. DO NOT include any HTML tags like `<div>` or `<h3>` in the regex since they do not exist in the source JS text.
3. Ensure every opened group `(?:` or `(` has a matching closing parenthesis `)` to avoid compilation syntax errors.
4. Set `move_to_jd=0` since descriptions are inline inside the JS objects.
"""

# ── Few-shot examples (real TechOps configs from OMS data) ───────────────────────

_FEW_SHOT_EXAMPLES = r"""\
EXAMPLE 1 — POST/AJAX HTML endpoint, MOVE_TO_JD=1:
Source URL: https://routemobile.com/jm-ajax/get_listings/ (POST, body: lang=&search_keywords=)
LOCRGX: (?s)t<a href=\\"https.+?job\\/(([^\\\\]+)).+?position[^>]+>[^>]+>([^<]+).+?location[^>]+>\\n\\t\\t\\t([^\\\\]+)
LOCRGXSEQ: JOBID,JOBLINK,JOBTITLE,LOCATION
MOVE_TO_JD: 1
JDRGX1: (?s)<div class="job_description">[^>]+>[^>]+>[^>]+>[^>]+>[^>]+>[^>]+>[^>]+>(.+?)Apply for
JDRGXSEQ1: JOBDESC

EXAMPLE 2 — GET with paginated custom URL, MOVE_TO_JD=1:
Source URL: https://careers.exfo.com/search/?q=india&startrow=!0o!STARTJOBNO!0o!
LOCRGX: (?s)<a class="jobTitle-link"[^"]+\"(([^"]+))">([^<]+)[^>]+>[^>]+>[^>]+>[^>]+>([^<]+)[^>]+>[^>]+>[^>]+>([^<]+)
LOCRGXSEQ: JOBLINK,JOBID,JOBTITLE,LOCATION,Date
MOVE_TO_JD: 1
JDRGX1: (?s)class="rtltextaligneligible">(.+?)Apply now
JDRGXSEQ1: JOBDESC

EXAMPLE 3 — GET direct career page, MOVE_TO_JD=0 (full description inline in accordion/card):
Source URL: https://extremewebworld.com/career-with-us.html
LOCRGX: (?s)<div\s+class="accordion-item"[^>]*>\s*<h2[^>]*>\s*<button[^>]*>\s*(([^<]*?\S))\s*</button>\s*</h2>\s*<div[^>]*class="[^"]*accordion-collapse[^"]*"[^>]*>\s*<div[^>]*class="[^"]*accordion-body[^"]*"[^>]*>\s*(.*?)\s*</div>\s*</div>\s*</div>
LOCRGXSEQ: JOBID,JOBTITLE,JOBDESC
MOVE_TO_JD: 0

EXAMPLE 4 — Zoho Recruit hosted site (embedded JSON in static HTML), MOVE_TO_JD=1:
Source URL: https://metizsoft.zohorecruit.com/jobs/Careers
LOCRGX: (?s)(?=(?:&#34;|&quot;)Posting_Title(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)City(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Work_Experience(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)id(?:&#34;|&quot;):\s*(?:&#34;|&quot;)(\d+)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Date_Opened(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;))(?:&#34;|&quot;)Posting_Title(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)City(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Work_Experience(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)id(?:&#34;|&quot;):\s*(?:&#34;|&quot;)(\d+)(?:&#34;|&quot;)[^}]*?(?:&#34;|&quot;)Date_Opened(?:&#34;|&quot;):\s*(?:&#34;|&quot;)((?:(?!&#34;|&quot;).)+?)(?:&#34;|&quot;)
LOCRGXSEQ: JOBTITLE,LOCATION,EXPERIENCE,JOBLINK,Date
MOVE_TO_JD: 1
"""

_PROMPT_TEMPLATE = r"""\
You are a JPERL config engineer at Naukri. Your job is to write PCRE regex patterns (or JSON paths) \
that extract job listings from static raw HTTP responses.

{examples}

Note: Few-shot examples are structural illustrations only. Do NOT copy patterns, field names, or move_to_jd values from examples. Always analyze the target HTML independently and determine move_to_jd from the actual page structure.

NOW YOUR TASK:
Career site URL: {career_url}
HTML source URL: {source_url}
HTML (truncated to {html_len} chars):
---
{html_snippet}
---

Return ONLY a valid JSON object — no markdown, no prose, no explanation:
{{
  "locrgx": "(?s)<PCRE pattern with capture groups>",
  "locrgxseq": "FIELD1,FIELD2,...",
  "move_to_jd": 1,
  "max_pages": "1",
  "confidence": 0.90
}}

Rules:
- Start pattern with (?s) for DOTALL mode.
- Avoid chained '.*?' or '.+?' wildcards. Anchor captures directly to explicit HTML tags (e.g. <a href="([^"]+)">([^<]+)</a>) or JSON keys.
- Capture groups must appear in LOCRGXSEQ order. The order of fields in LOCRGXSEQ MUST match the physical order of appearance of those fields in the HTML document flow (from top to bottom).
- LOCRGXSEQ values: JOBTITLE (mandatory), JOBLINK (detail link/ID), LOCATION, EXPERIENCE, Date, JOBDESC.
- JOBID (Job Code / Requisition ID / Reference ID):
  1. Capture `JOBID` if a distinct job code, requisition ID, or reference number physically exists in the job listing or container (e.g. `data-job-id="12345"`, `Job ID: REQ-1029`, `JR-2024-91`, `#10492`, `"id": "592523000005008001"`).
  2. NEVER capture javascript click handlers (`onclick="apply(1)"`), accordion/tab indices (`tab-1`, `item-box`), CSS classes, UI container IDs, or pagination numbers as `JOBID`.
  3. If no distinct job code or requisition ID is present on the page, OMIT `JOBID` from `LOCRGXSEQ`. The system automatically falls back to `JOBLINK` or the job-specific `JOBTITLE`.
- LOCATION & EXPERIENCE Extraction Boundaries:
  1. Capture `LOCATION` and `EXPERIENCE` ONLY if they exist as distinct, structured metadata badges, table cells, or JSON attributes in the raw document (e.g. `<td>10yrs+</td>`, `<span class="location">`, `"City": "..."`, `"Work_Experience": "..."`).
  2. If `LOCATION` or `EXPERIENCE` is NOT present as a distinct structured tag/key on the page, OMIT them completely from `LOCRGXSEQ`. Never invent, guess, or force-create a field from narrative text or sentences.
  3. Never slice `EXPERIENCE` or `LOCATION` out of an unstructured job description or paragraph. If experience/location is mentioned within a paragraph or list of requirements, leave it inside `JOBDESC` (never split or truncate `JOBDESC`).
  4. Always capture clean inner text only (e.g. `>([^<]+)<` or `>\s*([^<]+?)\s*<`). NEVER capture enclosing HTML tags (`<span...>`, `<div>`) inside `JOBTITLE`, `LOCATION`, or `EXPERIENCE`.
- JOBLINK & MOVE_TO_JD Priority Rules:
  1. Priority 1 (JOBLINK): If job detail links/pages exist (or the site provides unique job link paths/IDs like Zoho Recruit), ALWAYS capture the link/id into JOBLINK and set `move_to_jd=1`.
  2. Priority 2: If the full job description is already fully visible inline on the listing page card/accordion, capture it into `JOBDESC` and set `move_to_jd=0`.
  3. Priority 3: If jobs are displayed statically on a single page with no individual detail links/pages (and no separate detail URLs exist), omit JOBLINK, capture visible fields, and set `move_to_jd=0`.
- max_pages: "1" if single page, "3" if pagination detected.
- confidence: 0.9 if you can clearly see ≥2 repeating job elements; 0.5 if structure is unclear; 0.2 if guessing.
- **Single-Page / Paginated Batch Awareness**:
  1. The HTML snippet represents the current listing page/batch (typically 10-25 jobs). Matching the repeating job structures on this page is expected.
  2. Do NOT broaden the pattern to match non-job headers, footers, or navigation items.
- **Write robust and flexible regex patterns**:
  1. Use `<tag[^>]*>` instead of `<tag>` (e.g. use `<h2[^>]*>` instead of `<h2>`, `<a[^>]*>` instead of `<a>`, `<div[^>]*>` instead of `<div>`).
  2. Handle dynamic or varying classes/IDs using wildcards: `<div[^>]*class="[^"]*your_class[^"]*"[^>]*>`.
  3. Use `\s*` or `.*?` to handle variable spaces, tabs, and newlines between HTML elements.
  4. Match URLs and quotes flexibly (e.g. href="([^"]+)" or href='([^']+)'). NEVER append character class boundaries like [^>] or [^\>]+ immediately after the closing quote of a URL capture group.
  5. If the jobs are serialized inside an `application/ld+json` script block, JavaScript variables, or a hidden input element's JSON value (for example, Zoho Recruit, Phenom People, or Next.js state variables), target those JSON/variable patterns directly.
"""

_RETRY_PROMPT_TEMPLATE = """\
You are an expert JPERL config engineer. Your previous extraction attempt failed.
Do NOT simply tweak the previous pattern. First DIAGNOSE the source representation, choose the right strategy, and generate the pattern.

Career site URL: {career_url}
HTML source URL: {source_url}

Failed regex: {failed_regex}
Failed locrgxseq: {failed_seq}
Reason: {failure_reason}

HTML snippet (truncated):
---
{html_snippet}
---

DIAGNOSTIC WORKFLOW:
1. DIAGNOSE SOURCE REPRESENTATION:
   - Type A (HTML DOM): Repeating HTML elements (<div class="job-card">, <tr>, <li>).
   - Type B (Embedded / Serialized JSON): Look for <script type="application/ld+json">, <script id="__NEXT_DATA__">, window.__INITIAL_STATE__, or var jobs = [...].
   - Type C (Escaped JSON): Look for &quot;Posting_Title&quot;, &#34;title&#34;, or \\"title\\": \\"...\\".
   - Type D (Data Attributes): Look for data-job="..." or hidden inputs.

2. CHOOSE STRATEGY:
   - If clean JSON structure exists -> Return "locjson" and "locjsonseq".
   - If embedded/escaped JSON or JS state -> Write "locrgx" targeting JSON key-value patterns (e.g. (?s)"title":\\s*"([^"]+)").
   - If HTML DOM -> Write "locrgx" anchoring to explicit tags.

3. REPAIR SPECIFIC FAILURE:
   - If previous error was "0 matches / No jobs extracted": Do NOT broaden regex; check if data lives in <script> or JSON blocks.
   - If previous error was "LOCATION/EXPERIENCE too long or contains HTML": Match innermost text (>([^<]+)<) or OMIT optional fields (LOCATION/EXPERIENCE/Date) from locrgxseq.
   - If previous error was "Capture groups count mismatch": Ensure every capture group (...) has exactly one matching field in locrgxseq.

4. MOVE_TO_JD:
   - If detail links/pages or unique IDs exist -> capture JOBLINK in sequence and set move_to_jd=1.
   - If full description is inline or single page -> set move_to_jd=0.

Return ONLY valid JSON (no markdown, no prose):
{{
  "locrgx": "(?s)<PCRE pattern with capture groups>",
  "locrgxseq": "COMMA_SEPARATED_FIELDS",
  "move_to_jd": 1,
  "max_pages": "1",
  "confidence": 0.90
}}
"""

_RETRY_1_PROMPT_TEMPLATE = """\
You are an expert JPERL config engineer. Your previous extraction attempt failed verification.
Do NOT simply tweak the previous pattern. First DIAGNOSE the source representation, choose the right strategy, and generate the pattern.

Career site URL: {career_url}
HTML source URL: {source_url}
HTML (truncated to {html_len} chars):
---
{html_snippet}
---

PREVIOUS ATTEMPT (Attempt 1):
Pattern: {failed_pattern}
Sequence: {failed_seq}
Validation Result: match_count = {failed_matches}, error = {failed_error}

DIAGNOSTIC WORKFLOW:
1. DIAGNOSE SOURCE REPRESENTATION:
   - Type A (HTML DOM): Repeating HTML elements (<div class="job-card">, <tr>, <li>).
   - Type B (Embedded / Serialized JSON): Look for <script type="application/ld+json">, <script id="__NEXT_DATA__">, window.__INITIAL_STATE__, or var jobs = [...].
   - Type C (Escaped JSON): Look for &quot;Posting_Title&quot;, &#34;title&#34;, or \\"title\\": \\"...\\".
   - Type D (Data Attributes): Look for data-job="..." or hidden inputs.

2. CHOOSE STRATEGY:
   - If clean JSON structure exists -> Return "locjson" and "locjsonseq".
   - If embedded/escaped JSON or JS state -> Write "locrgx" targeting JSON key-value patterns (e.g. (?s)"title":\\s*"([^"]+)").
   - If HTML DOM -> Write "locrgx" anchoring to explicit tags.

3. REPAIR SPECIFIC FAILURE:
   - If previous error was "0 matches / No jobs extracted": Do NOT broaden regex; check if data lives in <script> or JSON blocks.
   - If previous error was "LOCATION/EXPERIENCE too long or contains HTML": Match innermost text (>([^<]+)<) or OMIT optional fields (LOCATION/EXPERIENCE/Date) from locrgxseq.
   - If previous error was "Capture groups count mismatch": Ensure every capture group (...) has exactly one matching field in locrgxseq.

4. MOVE_TO_JD:
   - If detail links/pages or unique IDs exist -> capture JOBLINK in sequence and set move_to_jd=1.
   - If full description is inline or single page -> set move_to_jd=0.

Return ONLY a valid JSON object — no markdown, no prose, no explanation:
{{
  "locrgx": "(?s)<regex pattern matching repeating job listings>",
  "locrgxseq": "COMMA_SEPARATED_FIELDS",
  "move_to_jd": 1,
  "max_pages": "1",
  "confidence": 0.90
}}
"""

_RETRY_2_PROMPT_TEMPLATE = """\
You are an expert JPERL config engineer. Your previous TWO extraction attempts failed verification.
Treat both previous attempts as failed hypotheses. Re-analyze the target payload from first principles and pivot to an alternative representation.

Career site URL: {career_url}
HTML source URL: {source_url}
HTML (truncated to {html_len} chars):
---
{html_snippet}
---

FAILED ATTEMPTS:
Attempt 1: Pattern: {attempt1_pattern} (seq: {attempt1_seq}) -> Result: matches={attempt1_matches}, error={attempt1_error}
Attempt 2: Pattern: {attempt2_pattern} (seq: {attempt2_seq}) -> Result: matches={attempt2_matches}, error={attempt2_error}

ADVANCED FORENSIC PIVOT:
1. PIVOT SOURCE REPRESENTATION:
   - If both previous attempts targeted HTML tags and got 0 matches, the jobs are almost certainly inside a <script> block, application/ld+json, __NEXT_DATA__, JS array, or HTML-escaped JSON (&quot;). Target the serialized data instead!
   - If previous attempts extracted HTML tags into fields, target innermost clean text (>([^<]+)<) or remove optional secondary fields (EXPERIENCE, LOCATION, Date) to retain only JOBTITLE and JOBLINK.

2. CHOOSE STRATEGY:
   - If JSON format -> Output "locjson" + "locjsonseq".
   - If Regex format -> Output "locrgx" + "locrgxseq".

3. STRICT ALIGNMENT:
   - Exactly one capture group (...) per field in locrgxseq.
   - Set move_to_jd=1 if detail links exist (capture JOBLINK); else move_to_jd=0.

Now generate Attempt 3. Return ONLY a valid JSON object — no markdown, no prose, no explanation:
{{
  "locrgx": "(?s)<regex pattern matching repeating job listings>",
  "locrgxseq": "COMMA_SEPARATED_FIELDS",
  "move_to_jd": 1,
  "max_pages": "1",
  "confidence": 0.90
}}
"""

_PROMPT_WITH_JD = """\
The LOCRGX config requires JDRGX (job description regex) because move_to_jd=1.

Career URL: {career_url}
Job detail page URL: {jd_url}
Job detail page HTML (truncated):
---
{jd_html}
---

Write a PCRE regex to extract JOBDESC from this job detail page HTML.
Return ONLY valid JSON:
{{
  "jdrgx": "(?s)<PCRE pattern with one capture group>",
  "jdrgxseq": "JOBDESC"
}}

Rules:
- Single capture group for the full job description block.
- Start with (?s). Use (.+?) or ([^<]+) as appropriate.
- The description is usually in a <div> or <section> with class containing 'description', 'content', 'body', or 'jd'.
- **Write robust tag wrappers**: Use `<div[^>]*>` or `<section[^>]*>` instead of literal tag matches to handle dynamic attributes/classes and whitespace/newlines.
"""


class LOCRGXGenerator(PipelineStep):
    """
    Generates LOCRGX (HTML regex) or LOCJSON JPERL config from static HTTP responses.

    Executes progressive multi-attempt generation (Initial, Retry 1 Diagnostic, Retry 2 Forensic)
    against the exact raw HTTP payload. Safely routes to SRP if all 3 attempts fail validation.
    """

    def __init__(self, llm_client: Optional[LLMClient] = None) -> None:
        self._llm = llm_client or LLMClient()

    # ── PipelineStep interface ──────────────────────────────────────────────────

    def execute(self, inp: GeneratorInput, state: PipelineState) -> StepResult:
        # Check SourceResolver decision — skip only if already handled by ATS or JSON API
        if state.source_decision:
            if state.source_decision.source in (SourceType.ATS, SourceType.JSON_API) and state.detection_path in ("ats", "llm"):
                logger.info("LOCRGXGenerator: skipping — already handled by %s", state.detection_path)
                return StepResult(StepSignal.CONTINUE)
        else:
            # Skip if already handled by ATS fingerprinter (backward compatibility fallback)
            if state.detection_path == "ats":
                logger.info("LOCRGXGenerator: skipping — already handled by ATSFingerprinter")
                return StepResult(StepSignal.CONTINUE)

        # Skip if no HTML data available at all
        if not state.page_html and not state.html_candidates and not state.raw_static_html:
            logger.warning("LOCRGXGenerator: no HTML data available for %s", inp.career_site_url)
            state.output.tech_comments = (
                "LOCRGXGenerator: no rendered HTML or HTML XHR captured by Playwright. "
                "Signal: Playwright may have been blocked by auth/CAPTCHA/bot-wall. "
                "Reason: cannot generate regex without HTML source. "
                "TechOps action: manually inspect page, provide LOCRGX + LOCRGXSEQ."
            )
            return StepResult(StepSignal.HALT_FAIL, reason="no-html-data")

        # Select best HTML source (prefers raw static HTTP response for static JPERL crawler)
        raw_body, source_url, method, req_headers, req_body = self._select_source(
            state.html_candidates, state.page_html, inp.career_site_url, state
        )

        # Ground truth HTML for validation is the exact raw response body
        ground_truth_html = raw_body

        # Strip comments & base64 only for the LLM snippet token budget
        # Note: ground_truth_html remains intact for validation
        clean_for_snippet = re.sub(r'(?s)<!--.*?-->', '', ground_truth_html)
        clean_for_snippet = re.sub(r'data:[^;]+;base64,[A-Za-z0-9+/=\s]+', '', clean_for_snippet)
        html_snippet = self._trim_html(clean_for_snippet, source_url, state)

        logger.info(
            "LOCRGXGenerator: generating static extraction from %s (%d chars)",
            source_url or inp.career_site_url, len(html_snippet),
        )

        # ── Multi-attempt progressive self-healing loop (Initial + Retry 1 + Retry 2) ──
        max_attempts = 3
        attempt = 1
        result: Optional[_LLMRegexOutput] = None
        is_valid = False
        is_jd_complete = False
        err_msg = ""
        matches = 0
        job_list: list[dict] = []
        attempts_history: list[dict] = []

        from src.validation import validate_regex_jobs, validate_job_objects
        from src.extraction.replay_engine import ReplayEngine

        while attempt <= max_attempts:
            logger.info("LOCRGXGenerator: static extraction attempt %d/%d for %s", attempt, max_attempts, inp.career_site_url)
            
            if attempt == 1:
                # Attempt 1: Initial generation
                result = self._generate_locrgx(inp, html_snippet, source_url)
            elif attempt == 2:
                # Attempt 2: Retry 1 — Diagnostic correction
                prev1 = attempts_history[0]
                result = self._retry_1_generate_locrgx(
                    inp=inp,
                    html_snippet=html_snippet,
                    source_url=source_url,
                    failed_pattern=prev1["pattern"],
                    failed_seq=prev1["seq"],
                    failed_matches=prev1["matches"],
                    failed_error=prev1["error"],
                )
            else:
                # Attempt 3: Retry 2 — Advanced forensic correction
                prev1 = attempts_history[0]
                prev2 = attempts_history[1]
                result = self._retry_2_generate_locrgx(
                    inp=inp,
                    html_snippet=html_snippet,
                    source_url=source_url,
                    attempt1_pattern=prev1["pattern"],
                    attempt1_seq=prev1["seq"],
                    attempt1_matches=prev1["matches"],
                    attempt1_error=prev1["error"],
                    attempt2_pattern=prev2["pattern"],
                    attempt2_seq=prev2["seq"],
                    attempt2_matches=prev2["matches"],
                    attempt2_error=prev2["error"],
                )

            if result is None:
                err_msg = "LLM output could not be parsed as valid JSON matching LOCRGX or LOCJSON schema."
                logger.warning("LOCRGXGenerator: Attempt %d produced no parseable output", attempt)
                attempts_history.append({
                    "attempt": attempt,
                    "type": "unknown",
                    "pattern": "None",
                    "seq": "None",
                    "matches": 0,
                    "error": err_msg,
                })
                state.failed_static_attempts.append(attempts_history[-1])
                attempt += 1
                continue

            # Validate against the exact SAME ground_truth_html
            if result.locjson and result.locjsonseq:
                try:
                    config = {"LOCJSON": result.locjson, "LOCJSONSEQ": result.locjsonseq}
                    job_list = ReplayEngine.run(config, api_response=ground_truth_html, page_html=ground_truth_html)
                    is_valid, err_msg = validate_job_objects(job_list, inp.jobs_on_career_page)
                    matches = len(job_list)
                except Exception as e:
                    is_valid = False
                    err_msg = f"LOCJSON execution error: {e}"
                    matches = 0
                    job_list = []
            else:
                is_valid, err_msg, job_list = validate_regex_jobs(
                    result.locrgx or "", result.locrgxseq or "", ground_truth_html, inp.jobs_on_career_page
                )
                matches = len(job_list)

            is_jd_complete = self._check_jd_completeness(result, matches, ground_truth_html, inp.career_site_url, job_list=job_list)
            if not is_jd_complete:
                is_valid = False
                err_msg = (
                    "Captured JOBDESC is incomplete or missing from listing while separate job detail links exist. "
                    "Set move_to_jd=1 and capture JOBLINK in locrgxseq so full job descriptions can be extracted from detail pages."
                )
            elif is_valid and result.move_to_jd == 1:
                seq_fields = [f.strip() for f in (result.locjsonseq or result.locrgxseq or "").split(",") if f.strip()]
                has_joblink = any(f == "JOBLINK" or f.startswith("JOBLINK=") for f in seq_fields)
                if not has_joblink:
                    is_valid = False
                    err_msg = (
                        "move_to_jd=1 was set, but JOBLINK was not captured in sequence. "
                        "When detail pages exist, capture the detail href/url or id token as JOBLINK so the crawler can request detail pages via {{VARJOBLINK}}."
                    )

            current_attempt_record = {
                "attempt": attempt,
                "type": "locjson" if result.locjson else "locrgx",
                "pattern": str(result.locjson if result.locjson else (result.locrgx or "")),
                "seq": str(result.locjsonseq if result.locjsonseq else (result.locrgxseq or "")),
                "matches": matches,
                "error": err_msg if not is_valid else "OK",
            }
            attempts_history.append(current_attempt_record)
            state.failed_static_attempts.append(current_attempt_record)

            if is_valid and is_jd_complete:
                logger.info(
                    "LOCRGXGenerator: Attempt %d PASSED validation (type=%s, matches=%d)",
                    attempt, current_attempt_record["type"], matches
                )
                break
            else:
                logger.warning(
                    "LOCRGXGenerator: Attempt %d FAILED validation (matches=%d, error=%s)",
                    attempt, matches, err_msg
                )
                attempt += 1

        # ── Final safety condition ─────────────────────────────────────────────
        # If neither LOCRGX nor LOCJSON produced a valid extraction after all retries:
        if not is_valid or not is_jd_complete or matches == 0 or result is None:
            logger.warning(
                "LOCRGXGenerator: all %d static extraction attempts failed. Rejecting STATIC_HTML JPERL and falling back to XPath/browser automation.",
                len(attempts_history)
            )
            state.output.tech_comments = (
                f"LOCRGXGenerator: static extraction failed after {len(attempts_history)} attempts. "
                f"History: {json.dumps(attempts_history, default=str)}. "
                "Rejecting static JPERL path and falling back to XPath browser automation."
            )
            # Route to SRPAUTOMATION
            state.is_srp = True
            if state.source_decision:
                state.source_decision.source = SourceType.RENDERED_DOM
            return StepResult(StepSignal.CONTINUE, reason="static-html-rejected-fallback-to-srp")

        # ── Detail extraction (JDRGX / JDJSON) if MOVE_TO_JD=1 ────────────────
        if result.move_to_jd == 1:
            if result.locjson and result.locjsonseq:
                sample_urls = [j.get("JOBLINK", "").strip() for j in job_list if j.get("JOBLINK")]
            else:
                sample_urls = self._extract_sample_job_urls(
                    result.locrgx or "", result.locrgxseq or "", ground_truth_html, inp.career_site_url, state=state, job_list=job_list
                )

            if sample_urls:
                try:
                    jd_extractor = JDExtractor(self._llm)
                    jd_res = jd_extractor.extract(inp, sample_urls, request_headers=req_headers)
                    if jd_res.strategy == "JDRGX" and jd_res.jdrgx_patterns:
                        primary_pat = jd_res.jdrgx_patterns[0].get("pattern", "")
                        primary_seq = jd_res.jdrgx_patterns[0].get("seq", "JOBDESC")
                        result = result.model_copy(update={
                            "jdrgx": primary_pat,
                            "jdrgxseq": primary_seq,
                            "jdrgx_patterns": jd_res.jdrgx_patterns
                        })
                        logger.info("LOCRGXGenerator: JDRGX generated from %s (%d patterns)", sample_urls[0], len(jd_res.jdrgx_patterns))
                    elif jd_res.strategy == "JDJSON" and jd_res.jdjson_patterns:
                        result = result.model_copy(update={"jdjson_patterns": jd_res.jdjson_patterns})
                        logger.info("LOCRGXGenerator: JDJSON generated from %s (%d patterns)", sample_urls[0], len(jd_res.jdjson_patterns))
                    else:
                        logger.warning("LOCRGXGenerator: JDExtractor failed to extract verified detail patterns: %s", jd_res.error_msg)
                except Exception as exc:
                    logger.warning("LOCRGXGenerator: detail extraction encountered an unexpected error: %s", exc)
            else:
                logger.warning(
                    "LOCRGXGenerator: move_to_jd=1 but no valid JOBLINK sample URLs could be extracted from listing (LOCRGXSEQ: %s).",
                    result.locrgxseq or result.locjsonseq
                )

            if not (result.jdrgx or result.jdrgx_patterns or result.jdjson_patterns):
                if matches >= 3:
                    logger.warning(
                        "LOCRGXGenerator: move_to_jd=1 but detail page fetch/extraction yielded no JDRGX pattern (detail link may be external apply form or unreachable). "
                        "Adjusting config to move_to_jd=0 to retain %d verified listing job(s).",
                        matches
                    )
                    result.move_to_jd = 0
                else:
                    logger.warning(
                        "LOCRGXGenerator: move_to_jd=1 but no verified JDRGX or JDJSON detail pattern could be extracted. Falling back to XPath/browser automation."
                    )
                    state.output.tech_comments = (
                        "LOCRGXGenerator: move_to_jd=1 but no verified JDRGX or JDJSON detail pattern could be extracted. "
                        "Falling back to XPath browser automation."
                    )
                    state.is_srp = True
                    if state.source_decision:
                        state.source_decision.source = SourceType.RENDERED_DOM
                    return StepResult(StepSignal.CONTINUE, reason="jd-extraction-failed-fallback-to-srp")

        # ── Build final LOCRGXResult ───────────────────────────────────────────
        final = LOCRGXResult(
            source_url=source_url if source_url != inp.career_site_url else None,
            method=method,
            request_headers=req_headers,
            request_body=req_body,
            locrgx=result.locrgx,
            locrgxseq=result.locrgxseq,
            locjson=result.locjson,
            locjsonseq=result.locjsonseq,
            jdrgx=result.jdrgx,
            jdrgxseq=result.jdrgxseq,
            jdrgx_patterns=result.jdrgx_patterns,
            jdjson_patterns=result.jdjson_patterns,
            move_to_jd=result.move_to_jd,
            max_pages=result.max_pages,
            confidence=0.90,
        )
        state.locrgx_result  = final
        state.detection_path = "locrgx"
        state.last_prompt = getattr(self, "_last_prompt", None)

        # Populate Replay Engine results
        state.output.extracted_jobs = job_list
        state.output.replay_status = "PASSED"
        state.output.replay_error = None

        out = state.output
        out.tech_status      = TechStatus.DONE
        out.sub_tech_comment = SubTechComment.JOBS_NEW_POOL
        out.site_type        = SiteType.ATS
        out.crawler_type     = CrawlerType.JPERL
        out.confidence       = 0.90
        out.tech_comments    = (
            f"LOCRGXGenerator: static extraction matched {matches} listing(s) "
            f"({'LOCJSON' if final.locjson else 'LOCRGX'}), "
            f"confidence=0.90, MOVE_TO_JD={result.move_to_jd}."
        )

        return StepResult(StepSignal.HALT_OK, reason="locrgx-config-generated")

    # ── HTML source selection (deterministic, no LLM) ─────────────────────────

    @staticmethod
    def _select_source(
        candidates: list[HTMLCandidate],
        page_html: Optional[str],
        career_url: str,
        state: Optional[PipelineState] = None,
    ) -> tuple[str, Optional[str], str, dict, Optional[str]]:
        """
        Returns (html_body, source_url, method, request_headers, request_body).
        Priority: highest-scoring HTML XHR candidate (preferring paginated endpoints) → raw static HTML → rendered page HTML.
        """
        noise_patterns = [
            "cart", "checkout", "product", "account", "wishlist", "contact", 
            "about", "blog", "news", "event", "telemetry", "analytics", 
            "facebook", "google", "hotjar", "tawk", "clarity", "snapchat", 
            "search?type=product", "search?q=", "popup", "banner", "cookie",
            ".js", ".css", "widget", "nopaperforms"
        ]
        valid_candidates = []
        for c in (candidates or []):
            url_lower = c.url.lower()
            if not any(p in url_lower for p in noise_patterns) and c.job_signal_score >= 2:
                valid_candidates.append(c)

        # Safety Net: if html_candidates missed it, check state.captured for 2xx HTML responses with pagination
        if not valid_candidates and state and state.captured:
            for req in state.captured:
                if req.response_status and 200 <= req.response_status < 300 and req.response_body:
                    ct = req.response_headers.get("content-type", "").lower()
                    url_lower = req.url.lower()
                    if ("text/html" in ct or "<div" in req.response_body or "<table" in req.response_body) and not any(p in url_lower for p in noise_patterns):
                        param_info = PaginationDetector.detect_and_parametrize({
                            "url": req.url,
                            "method": req.method,
                            "request_body": req.request_body,
                            "request_headers": req.request_headers,
                        })
                        if param_info:
                            valid_candidates.append(HTMLCandidate(
                                url=req.url,
                                method=req.method,
                                request_headers=req.request_headers,
                                request_body=req.request_body,
                                html_body=req.response_body,
                                job_signal_score=20,
                            ))

        if valid_candidates:
            # Sort candidates prioritizing paginated endpoints first, then job_signal_score
            def _cand_sort_key(c: HTMLCandidate) -> tuple[int, int]:
                param_info = PaginationDetector.detect_and_parametrize({
                    "url": c.url,
                    "method": c.method,
                    "request_body": c.request_body,
                    "request_headers": c.request_headers,
                })
                has_pag = 1 if param_info is not None else 0
                return (has_pag, c.job_signal_score)

            valid_candidates.sort(key=_cand_sort_key, reverse=True)
            best = valid_candidates[0]
            logger.info(
                "LOCRGXGenerator: analyzing HTML XHR candidate (score=%d) %s",
                best.job_signal_score, best.url,
            )
            # Detect pagination parameters and parameterize with JPERL token
            param_info = PaginationDetector.detect_and_parametrize({
                "url": best.url,
                "method": best.method,
                "request_body": best.request_body,
                "request_headers": best.request_headers,
            })
            if param_info:
                logger.info(
                    "LOCRGXGenerator: parameterized pagination (%s in %s) -> URL: %s, POST: %s",
                    param_info.param_name, param_info.location,
                    param_info.parameterized_url, param_info.parameterized_body
                )
                return (
                    best.html_body,
                    param_info.parameterized_url,
                    param_info.original_method,
                    param_info.headers,
                    param_info.parameterized_body,
                )

            return best.html_body, best.url, best.method, best.request_headers, best.request_body

        # Check if landing URL has pagination parameter in query or path
        landing_param_info = PaginationDetector.detect_and_parametrize({
            "url": career_url,
            "method": "GET",
        })
        effective_career_url = landing_param_info.parameterized_url if landing_param_info else career_url

        is_rendered_dom = (
            state and state.source_decision and state.source_decision.source == SourceType.RENDERED_DOM
        )

        if state and state.raw_static_html and not is_rendered_dom:
            logger.info("LOCRGXGenerator: using raw_static_html from static fetch")
            return state.raw_static_html, effective_career_url, "GET", {}, None

        # Fallback: use rendered page HTML, URL = effective_career_url
        logger.info("LOCRGXGenerator: no HTML XHR (or RENDERED_DOM SPA) — using rendered page_html")
        return page_html or "", effective_career_url, "GET", {}, None

    @staticmethod
    def _trim_html(html: str, source_url: Optional[str] = None, state: Optional[PipelineState] = None) -> str:
        """
        Extract the most job-relevant section of HTML/JS to stay within token budget.
        """
        # If it's a JS file (no HTML markup or source URL ends with .js), use JS-specific keyword matching window
        is_js = False
        if source_url and source_url.split('?')[0].endswith('.js'):
            is_js = True
        else:
            stripped = html.strip()
            if (stripped.startswith("function") or stripped.startswith("const ") or 
                stripped.startswith("import ") or "assets/" in html or ".js" in html):
                if "<div" not in html and "<p" not in html and "<span" not in html:
                    is_js = True

        if is_js:
            js_patterns = [
                r'(?i)"title"\s*:',
                r'(?i)\btitle\s*:',
                r'(?i)"jobTitle"\s*:',
                r'(?i)\bjobTitle\s*:',
                r'(?i)"position"\s*:',
                r'(?i)\bposition\s*:',
                r'(?i)"role"\s*:',
                r'(?i)\brole\s*:'
            ]
            first_match = None
            for pattern in js_patterns:
                match = re.search(pattern, html)
                if match:
                    if first_match is None or match.start() < first_match:
                        first_match = match.start()
            if first_match is not None:
                start = max(0, first_match - 2000)
                return html[start : start + _MAX_HTML_CHARS]
            return html[:_MAX_HTML_CHARS]

        from src.utils import trim_html
        anchor_titles = state.extracted_job_titles if state else None
        return trim_html(html, max_chars=_MAX_HTML_CHARS, anchor_titles=anchor_titles)

    # ── LLM: regex generation ─────────────────────────────────────────────────

    def _generate_locrgx(
        self,
        inp: GeneratorInput,
        html_snippet: str,
        source_url: Optional[str],
    ) -> Optional["_LLMRegexOutput"]:
        """Call LLM with few-shot prompt. Parse and return structured output."""
        prompt = _PROMPT_TEMPLATE.format(
            examples=_FEW_SHOT_EXAMPLES,
            career_url=inp.career_site_url,
            source_url=source_url or inp.career_site_url,
            html_len=len(html_snippet),
            html_snippet=html_snippet,
        )
        is_zoho = "zohorecruit" in (inp.career_site_url or "").lower() or (source_url and "zohorecruit" in source_url.lower()) or ("Posting_Title" in html_snippet and ("City" in html_snippet or "Work_Experience" in html_snippet or "moduleMeta" in html_snippet))
        if is_zoho:
            prompt += _ZOHO_INSTRUCTION
        is_sapizon = "sapizon.com" in inp.career_site_url or (source_url and "sapizon.com" in source_url)
        if is_sapizon:
            prompt += _SAPIZON_INSTRUCTION
        is_webcooks = "webcooks.in" in inp.career_site_url or (source_url and "webcooks.in" in source_url)
        if is_webcooks:
            prompt += _WEBCOOKS_INSTRUCTION
        is_isi = "isisecurity.in" in inp.career_site_url or (source_url and "isisecurity.in" in source_url)
        if is_isi:
            prompt += _ISI_INSTRUCTION
        is_js_asset = source_url and source_url.split('?')[0].endswith('.js')
        if is_js_asset:
            prompt += _JS_INSTRUCTION
            
        self._last_prompt = prompt
        raw = self._llm.call(prompt, temperature=0.05)
        if not raw:
            return None
        return self._parse_locrgx_response(raw)

    def _retry_1_generate_locrgx(
        self,
        inp: GeneratorInput,
        html_snippet: str,
        source_url: Optional[str],
        failed_pattern: str,
        failed_seq: str,
        failed_matches: int,
        failed_error: str,
    ) -> Optional["_LLMRegexOutput"]:
        """Attempt 2 / Retry 1: Diagnostic correction."""
        prompt = _RETRY_1_PROMPT_TEMPLATE.format(
            career_url=inp.career_site_url,
            source_url=source_url or inp.career_site_url,
            html_len=len(html_snippet),
            html_snippet=html_snippet,
            failed_pattern=failed_pattern,
            failed_seq=failed_seq,
            failed_matches=failed_matches,
            failed_error=failed_error,
        )
        is_zoho = "zohorecruit" in (inp.career_site_url or "").lower() or (source_url and "zohorecruit" in source_url.lower()) or ("Posting_Title" in html_snippet and ("City" in html_snippet or "Work_Experience" in html_snippet or "moduleMeta" in html_snippet))
        if is_zoho:
            prompt += _ZOHO_INSTRUCTION
        is_sapizon = "sapizon.com" in inp.career_site_url or (source_url and "sapizon.com" in source_url)
        if is_sapizon:
            prompt += _SAPIZON_INSTRUCTION
        is_webcooks = "webcooks.in" in inp.career_site_url or (source_url and "webcooks.in" in source_url)
        if is_webcooks:
            prompt += _WEBCOOKS_INSTRUCTION
        is_isi = "isisecurity.in" in inp.career_site_url or (source_url and "isisecurity.in" in source_url)
        if is_isi:
            prompt += _ISI_INSTRUCTION
        is_js_asset = source_url and source_url.split('?')[0].endswith('.js')
        if is_js_asset:
            prompt += _JS_INSTRUCTION

        self._last_prompt = prompt
        raw = self._llm.call(prompt, temperature=0.05)
        if not raw:
            return None
        return self._parse_locrgx_response(raw)

    def _retry_2_generate_locrgx(
        self,
        inp: GeneratorInput,
        html_snippet: str,
        source_url: Optional[str],
        attempt1_pattern: str,
        attempt1_seq: str,
        attempt1_matches: int,
        attempt1_error: str,
        attempt2_pattern: str,
        attempt2_seq: str,
        attempt2_matches: int,
        attempt2_error: str,
    ) -> Optional["_LLMRegexOutput"]:
        """Attempt 3 / Retry 2: Advanced forensic correction."""
        prompt = _RETRY_2_PROMPT_TEMPLATE.format(
            career_url=inp.career_site_url,
            source_url=source_url or inp.career_site_url,
            html_len=len(html_snippet),
            html_snippet=html_snippet,
            attempt1_pattern=attempt1_pattern,
            attempt1_seq=attempt1_seq,
            attempt1_matches=attempt1_matches,
            attempt1_error=attempt1_error,
            attempt2_pattern=attempt2_pattern,
            attempt2_seq=attempt2_seq,
            attempt2_matches=attempt2_matches,
            attempt2_error=attempt2_error,
        )
        is_zoho = "zohorecruit" in (inp.career_site_url or "").lower() or (source_url and "zohorecruit" in source_url.lower()) or ("Posting_Title" in html_snippet and ("City" in html_snippet or "Work_Experience" in html_snippet or "moduleMeta" in html_snippet))
        if is_zoho:
            prompt += _ZOHO_INSTRUCTION
        is_sapizon = "sapizon.com" in inp.career_site_url or (source_url and "sapizon.com" in source_url)
        if is_sapizon:
            prompt += _SAPIZON_INSTRUCTION
        is_webcooks = "webcooks.in" in inp.career_site_url or (source_url and "webcooks.in" in source_url)
        if is_webcooks:
            prompt += _WEBCOOKS_INSTRUCTION
        is_isi = "isisecurity.in" in inp.career_site_url or (source_url and "isisecurity.in" in source_url)
        if is_isi:
            prompt += _ISI_INSTRUCTION
        is_js_asset = source_url and source_url.split('?')[0].endswith('.js')
        if is_js_asset:
            prompt += _JS_INSTRUCTION

        self._last_prompt = prompt
        raw = self._llm.call(prompt, temperature=0.05)
        if not raw:
            return None
        return self._parse_locrgx_response(raw)

    @staticmethod
    def _clean_json_regex_escapes(s: str) -> str:
        result = []
        i = 0
        n = len(s)
        while i < n:
            if s[i] == '\\':
                if i + 1 < n:
                    next_char = s[i+1]
                    if next_char in ['"', '\\', '/', 'b', 'f', 'n', 'r', 't']:
                        result.append('\\')
                        result.append(next_char)
                        i += 2
                    elif next_char == 'u' and i + 5 < n and all(c in '0123456789abcdefABCDEF' for c in s[i+2:i+6]):
                        result.append('\\')
                        result.append('u')
                        result.extend(s[i+2:i+6])
                        i += 6
                    else:
                        result.append('\\\\')
                        result.append(next_char)
                        i += 2
                else:
                    result.append('\\\\')
                    i += 1
            else:
                result.append(s[i])
                i += 1
        return "".join(result)

    @staticmethod
    def _wrap_first_group(pattern: str) -> str:
        """Finds the first capturing group and wraps it to duplicate it for nested mappings."""
        in_escape = False
        for i, char in enumerate(pattern):
            if char == '\\':
                in_escape = not in_escape
                continue
            if char == '(' and not in_escape:
                if i + 1 < len(pattern) and pattern[i+1] != '?':
                    depth = 1
                    for j in range(i + 1, len(pattern)):
                        if pattern[j] == '\\':
                            continue
                        if pattern[j] == '(':
                            depth += 1
                        elif pattern[j] == ')':
                            depth -= 1
                            if depth == 0:
                                return pattern[:i] + '(' + pattern[i:j+1] + ')' + pattern[j+1:]
            in_escape = False
        return pattern

    @staticmethod
    def _parse_locrgx_response(raw: str) -> Optional["_LLMRegexOutput"]:
        try:
            clean = raw.strip()
            # Find first '{' and last '}' to extract JSON block (robust against model preambles/conversations)
            start_idx = clean.find("{")
            end_idx = clean.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean = clean[start_idx : end_idx + 1]
            clean = LOCRGXGenerator._clean_json_regex_escapes(clean)
            data = json.loads(clean)

            # Check if LOCJSON format was returned
            locjson = (data.get("locjson") or "").strip()
            locjsonseq = (data.get("locjsonseq") or "").strip()
            if locjson and locjsonseq:
                fields = [f.strip() for f in locjsonseq.split(",")]
                valid_fields = [f for f in fields if f.split("=")[0].strip() in _VALID_SEQ_FIELDS]
                if valid_fields:
                    logger.info("LOCRGXGenerator: LLM generated LOCJSON: %s | seq: %s | move_to_jd: %s", locjson, ",".join(valid_fields), data.get("move_to_jd"))
                    return _LLMRegexOutput(
                        locrgx=None,
                        locrgxseq=None,
                        locjson=locjson,
                        locjsonseq=",".join(valid_fields),
                        move_to_jd=int(data.get("move_to_jd", 0)),
                        jdrgx=data.get("jdrgx") or None,
                        jdrgxseq=data.get("jdrgxseq") or None,
                        jdrgx_patterns=data.get("jdrgx_patterns") or [],
                        jdjson_patterns=data.get("jdjson_patterns") or [],
                        max_pages=str(data.get("max_pages", "1")),
                        confidence=float(data.get("confidence", 0.0)),
                    )

            # Check LOCRGX format
            locrgx = data.get("locrgx", "").strip()
            locrgxseq = data.get("locrgxseq", "").strip()
            if not locrgx or not locrgxseq:
                logger.warning("LOCRGXGenerator: LLM returned empty locrgx/locrgxseq and empty locjson/locjsonseq")
                return None
            # Validate field sequence
            fields = [f.strip() for f in locrgxseq.split(",")]
            valid_fields = [f for f in fields if f in _VALID_SEQ_FIELDS]
            if not valid_fields:
                logger.warning("LOCRGXGenerator: no valid LOCRGXSEQ fields: %s", locrgxseq)
                return None

            # Self-healing: if the group count is off by exactly 1, try wrapping the first group
            try:
                compiled = re.compile(locrgx, re.DOTALL)
                if compiled.groups == len(valid_fields) - 1:
                    wrapped = LOCRGXGenerator._wrap_first_group(locrgx)
                    if re.compile(wrapped, re.DOTALL).groups == len(valid_fields):
                        logger.info("LOCRGXGenerator: Self-healed capture group mismatch by wrapping first group.")
                        locrgx = wrapped
            except Exception as e:
                logger.warning("LOCRGXGenerator: group mismatch self-healing helper failed: %s", e)

            logger.info("LOCRGXGenerator: LLM generated regex: %s | seq: %s | move_to_jd: %s", locrgx, ",".join(valid_fields), data.get("move_to_jd"))
            return _LLMRegexOutput(
                locrgx=locrgx,
                locrgxseq=",".join(valid_fields),
                locjson=None,
                locjsonseq=None,
                move_to_jd=int(data.get("move_to_jd", 0)),
                jdrgx=data.get("jdrgx") or None,
                jdrgxseq=data.get("jdrgxseq") or None,
                jdrgx_patterns=data.get("jdrgx_patterns") or [],
                jdjson_patterns=data.get("jdjson_patterns") or [],
                max_pages=str(data.get("max_pages", "1")),
                confidence=float(data.get("confidence", 0.0)),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("LOCRGXGenerator: failed to parse LLM response (%s): %s", exc, raw[:200])
            return None

    # ── Regex validation ──────────────────────────────────────────────────────

    @staticmethod
    def _validate_regex(pattern: str, html: str) -> int:
        """Returns number of matches. 0 = reject."""
        try:
            from src.extraction.regex_parser import safe_regex_findall
            return len(safe_regex_findall(pattern, html, timeout_s=2.0))
        except TimeoutError:
            logger.warning("LOCRGXGenerator: regex validation timed out (>2.0s): %s", pattern[:80])
            return 0
        except Exception as exc:
            logger.warning("LOCRGXGenerator: invalid regex (%s): %s", exc, pattern[:80])
            return 0

    @staticmethod
    def _is_jd_complete(jd_text: Optional[str]) -> bool:
        """Determines if the captured JD is complete and of high quality."""
        if not jd_text:
            return False
        # Clean HTML tags to get raw text count
        raw_text = re.sub(r'<[^>]*>', '', jd_text).strip()
        words = raw_text.split()
        if len(words) < 100 and len(raw_text) < 600:
            return False
        
        # Check for standard descriptive JPERL keywords (at least 2 matches)
        keywords = ["responsibilit", "qualification", "requirement", "experience", "skills", "benefit", "description", "role"]
        lower_text = raw_text.lower()
        matches = sum(1 for kw in keywords if kw in lower_text)
        if matches < 2:
            return False
            
        return True

    def _check_jd_completeness(
        self,
        result: _LLMRegexOutput,
        matches: int,
        unescaped_html: str,
        career_url: str,
        job_list: Optional[list[dict]] = None
    ) -> bool:
        if matches == 0 or result.move_to_jd == 1:
            return True
        seq = result.locjsonseq if result.locjson else (result.locrgxseq or "")
        fields = [f.strip() for f in seq.split(",") if f.strip()]
        
        has_distinct_joblink = False
        captured_jd = None
        
        if result.locrgx:
            if job_list:
                first = job_list[0]
                raw_link = first.get("JOBLINK", "").strip()
                if raw_link and not raw_link.startswith("#") and not raw_link.lower().startswith("javascript:") and not raw_link.lower().startswith("mailto:"):
                    jd_url = urljoin(career_url, raw_link)
                    parsed_career = urlparse(career_url)
                    parsed_jd = urlparse(jd_url)
                    is_distinct = (parsed_jd.netloc != parsed_career.netloc) or (parsed_jd.path.strip("/") != parsed_career.path.strip("/"))
                    if is_distinct and parsed_jd.path.strip("/") != "":
                        has_distinct_joblink = True
                captured_jd = first.get("JOBDESC")
            else:
                from src.extraction.regex_parser import safe_regex_findall
                try:
                    raw_matches = safe_regex_findall(result.locrgx, unescaped_html, timeout_s=2.0)
                except Exception:
                    raw_matches = []
                if not raw_matches:
                    return True
                first = raw_matches[0]
                
                # Check JOBLINK
                if "JOBLINK" in fields:
                    jl_idx = fields.index("JOBLINK")
                    raw_link = first[jl_idx] if isinstance(first, tuple) and jl_idx < len(first) else (first if jl_idx == 0 else None)
                    if raw_link:
                        raw_link = str(raw_link).strip()
                        if raw_link and not raw_link.startswith("#") and not raw_link.lower().startswith("javascript:") and not raw_link.lower().startswith("mailto:"):
                            jd_url = urljoin(career_url, raw_link)
                            parsed_career = urlparse(career_url)
                            parsed_jd = urlparse(jd_url)
                            is_distinct = (parsed_jd.netloc != parsed_career.netloc) or (parsed_jd.path.strip("/") != parsed_career.path.strip("/"))
                            if is_distinct and parsed_jd.path.strip("/") != "":
                                has_distinct_joblink = True
                
                # Check JOBDESC
                if "JOBDESC" in fields:
                    jd_idx = fields.index("JOBDESC")
                    captured_jd = first[jd_idx] if isinstance(first, tuple) and jd_idx < len(first) else (first if jd_idx == 0 else None)

        if "JOBDESC" not in fields:
            # If no JOBDESC in listing, but distinct job detail links exist -> reject move_to_jd=0, require move_to_jd=1
            if has_distinct_joblink:
                return False
            return True
            
        if captured_jd is None or not self._is_jd_complete(captured_jd):
            if has_distinct_joblink:
                return False
        return True

    # ── JDRGX / JDJSON: extract job URLs and fetch JD pages ─────────────────

    @staticmethod
    def _extract_sample_job_urls(
        locrgx: str,
        locrgxseq: str,
        html: str,
        career_url: str,
        max_samples: int = 3,
        state: Optional[PipelineState] = None,
        job_list: Optional[list[dict]] = None
    ) -> list[str]:
        """Extract up to max_samples JOBLINK URLs from the regex matches to fetch sample JD pages."""
        try:
            urls: list[str] = []

            # 1. Direct extraction from already parsed job_list
            if job_list:
                for j in job_list[:max_samples * 2]:
                    raw_link = j.get("JOBLINK", "").strip()
                    if raw_link and not raw_link.startswith(("#", "javascript:", "mailto:")):
                        if raw_link.isdigit() and len(raw_link) >= 10:
                            full_url = urljoin(career_url, f"/jobs/Careers/{raw_link}")
                        else:
                            full_url = raw_link if raw_link.startswith("http") else urljoin(career_url, raw_link)
                        if full_url not in urls:
                            urls.append(full_url)
                        if len(urls) >= max_samples:
                            return urls

                    # Try fallback to numeric or zohorecruit JOBID
                    raw_id = j.get("JOBID", "").strip()
                    if raw_id:
                        if "zohorecruit" in career_url or "jobs/Careers" in career_url or (raw_id.isdigit() and len(raw_id) >= 10):
                            full_url = urljoin(career_url, f"/jobs/Careers/{raw_id}")
                            if full_url not in urls:
                                urls.append(full_url)
                        if len(urls) >= max_samples:
                            return urls
                if urls:
                    return urls[:max_samples]

            fields = [f.strip() for f in locrgxseq.split(",") if f.strip()]
            from src.extraction.regex_parser import safe_regex_findall

            # 2. Fallback to safe_regex_findall with timeout
            if locrgx and ("JOBLINK" in fields or "JOBID" in fields):
                try:
                    matches = safe_regex_findall(locrgx, html, timeout_s=2.0)
                except Exception:
                    matches = []

                if "JOBLINK" in fields and matches:
                    jl_idx = fields.index("JOBLINK")
                    for m in matches[:max_samples]:
                        raw_link = m[jl_idx] if isinstance(m, tuple) and jl_idx < len(m) else (m if jl_idx == 0 else "")
                        raw_link = str(raw_link).strip()
                        if raw_link:
                            if raw_link.isdigit() and len(raw_link) >= 10:
                                full_url = urljoin(career_url, f"/jobs/Careers/{raw_link}")
                            else:
                                full_url = raw_link if raw_link.startswith("http") else urljoin(career_url, raw_link)
                            if full_url not in urls:
                                urls.append(full_url)
                    if urls:
                        return urls[:max_samples]

                if "JOBID" in fields and matches:
                    jid_idx = fields.index("JOBID")
                    for m in matches[:max_samples]:
                        raw_id = m[jid_idx] if isinstance(m, tuple) and jid_idx < len(m) else (m if jid_idx == 0 else "")
                        raw_id = str(raw_id).strip()
                        if raw_id:
                            if "zohorecruit" in career_url or "jobs/Careers" in career_url or (raw_id.isdigit() and len(raw_id) >= 10):
                                full_url = urljoin(career_url, f"/jobs/Careers/{raw_id}")
                            elif "/" in raw_id or raw_id.startswith("http"):
                                full_url = raw_id if raw_id.startswith("http") else urljoin(career_url, raw_id)
                            else:
                                full_url = urljoin(career_url, raw_id)
                            if full_url not in urls:
                                urls.append(full_url)
                    if urls:
                        return urls[:max_samples]

            # 3. Check state sample_jobs
            if state and state.source_decision and state.source_decision.sample_jobs:
                for sj in state.source_decision.sample_jobs:
                    link = sj.get("JOBLINK") or sj.get("raw_href")
                    if link and not link.startswith(("#", "javascript:")):
                        full_url = link if link.startswith("http") else urljoin(career_url, link)
                        if full_url not in urls:
                            urls.append(full_url)
                if urls:
                    return urls[:max_samples]

            return urls
        except Exception as exc:
            logger.debug("LOCRGXGenerator: sample JOBLINK extraction failed: %s", exc)
            return []

    @staticmethod
    def _extract_first_job_url(
        locrgx: str, locrgxseq: str, html: str, career_url: str
    ) -> Optional[str]:
        """Extract the first JOBLINK from the regex match to fetch the JD page."""
        samples = LOCRGXGenerator._extract_sample_job_urls(locrgx, locrgxseq, html, career_url, max_samples=1)
        return samples[0] if samples else None

    def _generate_jdrgx(self, inp: GeneratorInput, jd_url: str) -> tuple[Optional[str], Optional[str]]:
        """Fetch JD page HTML and ask LLM to generate JDRGX."""
        jd_html = self._fetch_jd_html(jd_url)
        if not jd_html:
            logger.warning("LOCRGXGenerator: could not fetch JD page %s", jd_url)
            return None, None

        prompt = _PROMPT_WITH_JD.format(
            career_url=inp.career_site_url,
            jd_url=jd_url,
            jd_html=jd_html[:_MAX_HTML_CHARS],
        )
        raw = self._llm.call(prompt, temperature=0.05)
        if not raw:
            return None, None
        try:
            clean = raw.strip()
            # Find first '{' and last '}' to extract JSON block (robust against preambles)
            start_idx = clean.find("{")
            end_idx = clean.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean = clean[start_idx : end_idx + 1]
            clean = LOCRGXGenerator._clean_json_regex_escapes(clean)
            data = json.loads(clean)
            jdrgx = (data.get("jdrgx") or "").strip()
            jdrgxseq = (data.get("jdrgxseq") or "JOBDESC").strip()
            if not jdrgx:
                return None, None
            return jdrgx, jdrgxseq
        except Exception as exc:
            logger.warning("LOCRGXGenerator: JDRGX parse failed (%s)", exc)
            return None, None

    @staticmethod
    def _fetch_jd_html(url: str) -> Optional[str]:
        """Simple GET to fetch JD page HTML for JDRGX generation."""
        try:
            resp = requests.get(
                url,
                timeout=_JD_FETCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 NaukriBot/1.0"},
                allow_redirects=True,
                verify=False,
            )
            if resp.status_code == 200:
                return resp.text
        except Exception as exc:
            logger.debug("LOCRGXGenerator: JD fetch error (%s) for %s", exc, url)
        return None


# ── Internal data class (not exported) ───────────────────────────────────────────

class _LLMRegexOutput:
    """Raw parsed output from LLM regex/json prompt."""
    __slots__ = (
        "locrgx", "locrgxseq", "locjson", "locjsonseq",
        "move_to_jd", "jdrgx", "jdrgxseq", "jdrgx_patterns", "jdjson_patterns",
        "max_pages", "confidence"
    )

    def __init__(
        self,
        locrgx,
        locrgxseq,
        move_to_jd,
        max_pages="1",
        confidence=0.90,
        jdrgx=None,
        jdrgxseq=None,
        locjson=None,
        locjsonseq=None,
        jdrgx_patterns=None,
        jdjson_patterns=None
    ):
        self.locrgx          = locrgx
        self.locrgxseq       = locrgxseq
        self.locjson         = locjson
        self.locjsonseq      = locjsonseq
        self.move_to_jd      = move_to_jd
        self.jdrgx           = jdrgx
        self.jdrgxseq        = jdrgxseq
        self.jdrgx_patterns  = jdrgx_patterns or []
        self.jdjson_patterns = jdjson_patterns or []
        self.max_pages       = max_pages
        self.confidence      = confidence

    def model_copy(self, update: dict) -> "_LLMRegexOutput":
        vals = {s: getattr(self, s, None) for s in self.__slots__}
        vals.update(update)
        return _LLMRegexOutput(**vals)


