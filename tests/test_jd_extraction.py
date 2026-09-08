"""
test_jd_extraction.py
─────────────────────
Unit tests for JD detection, extraction, compilation, and validation.
Tests:
  - JDDetector: payload classification, adaptive windowing, semantic validation
  - JsonParser.execute_jd: JDJSON1..N execution
  - RegexParser.execute_jd: JDRGX1..N execution
  - Compiler: multi-field JDJSON / JDRGX emission for all 4 combinations
  - JDExtractor: mock extraction and self-healing validation retry
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from src.compiler import Compiler
from src.extraction.json_parser import JsonParser
from src.extraction.regex_parser import RegexParser
from src.extraction.replay_engine import ReplayEngine
from src.jd_detector import JDDetector
from src.jd_extractor import JDExtractor, JDExtractionResult
from src.models import (
    GeneratorInput,
    LLMExtractionResult,
    LOCRGXResult,
    PaginationInfo,
)


# ── 1. JDDetector Tests ─────────────────────────────────────────────────────────

def test_jd_detector_payload_classification():
    detector = JDDetector.get_instance()

    # 1. Pure JSON
    assert detector.detect_payload_type('{"data": {"description": "Software Engineer"}}', "application/json") == "JSON"
    assert detector.detect_payload_type('[{"id": 1, "body": "Full job description"}]') == "JSON"

    # 2. HTML with embedded JSON
    embedded_html = """
    <html>
      <head>
        <script type="application/ld+json">{"@type": "JobPosting", "description": "Full JD"}</script>
      </head>
      <body><div>Careers</div></body>
    </html>
    """
    assert detector.detect_payload_type(embedded_html, "text/html") == "HTML_WITH_EMBEDDED_JSON"

    # 3. Pure HTML
    pure_html = "<html><body><div class='job-description'><h1>Dev</h1><p>Requirements: Python</p></div></body></html>"
    assert detector.detect_payload_type(pure_html, "text/html") == "HTML"

    # 4. Empty
    assert detector.detect_payload_type("", None) == "EMPTY"


def test_jd_detector_adaptive_windowing():
    detector = JDDetector.get_instance()

    # Create HTML with boilerplate at start, JD in middle, and Apply anchor
    filler_head = "<html><head><style>.a{color:red;}</style><script>console.log(1);</script></head><body>"
    nav_boilerplate = "<header><nav>Home | Careers | About Us</nav></header><div class='nav-space'>" + ("x" * 20000) + "</div>"
    jd_container = """
    <div class="job-description">
      <h2>About the Role</h2>
      <p>We are seeking a Senior Python Developer with 5+ years of experience in distributed systems.</p>
      <h3>Key Responsibilities</h3>
      <ul>
        <li>Build high performance data crawlers and APIs.</li>
        <li>Collaborate with cross-functional engineering teams.</li>
      </ul>
      <h3>Qualifications</h3>
      <p>Strong knowledge of Python, regex, and REST APIs.</p>
      <div class="apply-section">
        <a href="/apply/123" class="btn btn-primary">Apply Now</a>
      </div>
    </div>
    """
    filler_foot = "<div class='after-space'>" + ("y" * 15000) + "</div><footer><p>Copyright 2026 Privacy Policy</p></footer></body></html>"

    full_html = filler_head + nav_boilerplate + jd_container + filler_foot
    assert len(full_html) > 35000

    windowed, meta = detector.distill_and_window_html(full_html, min_chars=5000, max_chars=25000)

    # Verify boilerplate stripped
    assert "<style>" not in windowed
    assert "<header>" not in windowed
    assert "<footer>" not in windowed

    # Verify JD start and end anchors are present inside focused window
    assert "About the Role" in windowed
    assert "Apply Now" in windowed
    assert meta["strategy"] in ("start_and_end_anchors", "start_anchor_adaptive_span")


def test_jd_detector_validation():
    detector = JDDetector.get_instance()

    # Valid description
    valid_jd = (
        "About the Role: We are looking for a Software Engineer with responsibilities "
        "including building backend microservices, maintaining database integrity, and collaborating "
        "with frontend teams. Minimum qualifications include 3 years experience with Python and SQL."
    )
    is_valid, err = detector.validate_jd_text(valid_jd)
    assert is_valid is True
    assert err == ""

    # Too short
    is_valid, err = detector.validate_jd_text("Short text.")
    assert is_valid is False
    assert "too short" in err.lower()

    # Pure noise
    noise_text = "Privacy Policy. All rights reserved. Terms of use. Cookie policy. Contact us for questions." * 4
    is_valid, err = detector.validate_jd_text(noise_text)
    assert is_valid is False


# ── 2. Parser Execution Tests ──────────────────────────────────────────────────

def test_json_parser_execute_jd():
    config = {
        "JDJSON1": "data,longDescription|X|data,qualifications",
        "JDJSONSEQ1": "JOBDESC,JOBDESC",
        "JDJSON2": "data,datePosted",
        "JDJSONSEQ2": "Date"
    }
    payload = json.dumps({
        "data": {
            "longDescription": "Build cloud platforms and services.",
            "qualifications": "5+ years backend experience.",
            "datePosted": "2026-08-20"
        }
    })

    result = JsonParser.execute_jd(config, payload)
    assert "JOBDESC" in result
    assert "Build cloud platforms" in result["JOBDESC"]
    assert "5+ years backend experience" in result["JOBDESC"]
    assert result["Date"] == "2026-08-20"


def test_regex_parser_execute_jd():
    config = {
        "JDRGX1": r"(?s)<div class=\"description\">(.+?)</div>",
        "JDRGXSEQ1": "JOBDESC",
        "JDRGX2": r"(?s)Posted Date:\s*([^<]+)",
        "JDRGXSEQ2": "Date"
    }
    html = """
    <html>
      <body>
        <div class="description">
          <p>Lead technical design and scalable crawler infrastructure.</p>
        </div>
        <p>Posted Date: August 2026</p>
      </body>
    </html>
    """
    result = RegexParser.execute_jd(config, html)
    assert "JOBDESC" in result
    assert "Lead technical design" in result["JOBDESC"]
    assert result["Date"] == "August 2026"


def test_replay_engine_run_detail():
    config_json = {
        "JDJSON1": "job,description",
        "JDJSONSEQ1": "JOBDESC"
    }
    res_json = ReplayEngine.run_detail(config_json, '{"job": {"description": "Full job description text."}}')
    assert res_json.get("JOBDESC") == "Full job description text."

    config_rgx = {
        "JDRGX1": r"(?s)<h3>Description</h3>(.+?)<hr>",
        "JDRGXSEQ1": "JOBDESC"
    }
    res_rgx = ReplayEngine.run_detail(config_rgx, "<h3>Description</h3><p>Detailed role description.</p><hr>")
    assert "Detailed role description" in res_rgx.get("JOBDESC", "")


# ── 3. Compiler Multi-Field Combination Tests ───────────────────────────────────

def test_compiler_combinations():
    compiler = Compiler()
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    # 1. LOCRGX -> JDRGX (Standard HTML)
    loc_res1 = LOCRGXResult(
        locrgx=r"(?s)<li><a href='([^']+)'>([^<]+)",
        locrgxseq="JOBLINK,JOBTITLE",
        move_to_jd=1,
        jdrgx_patterns=[
            {"pattern": r"(?s)<div class=\"desc\">(.+?)</div>", "seq": "JOBDESC"},
            {"pattern": r"(?s)Location:\s*([^<]+)", "seq": "LOCATION"}
        ]
    )
    cfg1 = compiler.from_locrgx(inp, loc_res1)
    assert cfg1.body["MOVE_TO_JD"] == 1
    assert cfg1.body["JDRGX1"] == r"(?s)<div class=\"desc\">(.+?)</div>"
    assert cfg1.body["JDRGXSEQ1"] == "JOBDESC"
    assert cfg1.body["JDRGX2"] == r"(?s)Location:\s*([^<]+)"
    assert cfg1.body["JDRGXSEQ2"] == "LOCATION"

    # 2. LOCJSON -> JDJSON (Standard API)
    llm_res2 = LLMExtractionResult(
        api_url="https://api.example.com/jobs",
        field_jobtitle="title",
        field_jobid="id",
        field_joblink="id",
        move_to_jd=1,
        jdjson_patterns=[
            {"path": "data,details", "seq": "JOBDESC"},
            {"path": "data,postedOn", "seq": "Date"}
        ]
    )
    cfg2 = compiler.from_llm(inp, llm_res2)
    assert cfg2.body["MOVE_TO_JD"] == 1
    assert cfg2.body["JDJSON1"] == "data,details"
    assert cfg2.body["JDJSONSEQ1"] == "JOBDESC"
    assert cfg2.body["JDJSON2"] == "data,postedOn"
    assert cfg2.body["JDJSONSEQ2"] == "Date"

    # 3. LOCJSON -> JDRGX (API listing with frontend HTML detail page)
    llm_res3 = LLMExtractionResult(
        api_url="https://api.example.com/jobs",
        field_jobtitle="title",
        field_jobid="id",
        field_joblink="slug",
        move_to_jd=1,
        jdrgx_patterns=[
            {"pattern": r"(?s)<section id=\"job\">(.+?)</section>", "seq": "JOBDESC"}
        ]
    )
    cfg3 = compiler.from_llm(inp, llm_res3)
    assert cfg3.body["MOVE_TO_JD"] == 1
    assert cfg3.body["JDRGX1"] == r"(?s)<section id=\"job\">(.+?)</section>"
    assert cfg3.body["JDRGXSEQ1"] == "JOBDESC"

    # 4. LOCRGX -> JDJSON (HTML listing linking to detail JSON endpoint)
    loc_res4 = LOCRGXResult(
        locrgx=r"(?s)<a href='(/api/job/\d+)'>([^<]+)",
        locrgxseq="JOBLINK,JOBTITLE",
        move_to_jd=1,
        jdjson_patterns=[
            {"path": "jobDescription", "seq": "JOBDESC"}
        ]
    )
    cfg4 = compiler.from_locrgx(inp, loc_res4)
    assert cfg4.body["MOVE_TO_JD"] == 1
    assert cfg4.body["JDJSON1"] == "jobDescription"
    assert cfg4.body["JDJSONSEQ1"] == "JOBDESC"


# ── 4. JDExtractor End-to-End Mock Tests ───────────────────────────────────────

def test_jd_extractor_html_with_self_healing_retry():
    mock_llm = MagicMock()
    # First response is invalid (matches nothing), second response is valid
    mock_llm.call.side_effect = [
        # Call 1: invalid regex
        json.dumps({
            "strategy": "JDRGX",
            "patterns": [{"pattern": r"(?s)<div id=\"non_existent\">(.+?)</div>", "seq": "JOBDESC"}]
        }),
        # Call 2 (Retry): valid regex
        json.dumps({
            "strategy": "JDRGX",
            "patterns": [{"pattern": r"(?s)<div class=\"job-description\">(.+?)Apply Now", "seq": "JOBDESC"}]
        })
    ]

    extractor = JDExtractor(llm_client=mock_llm)
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    sample_html = """
    <html>
      <body>
        <div class="job-description">
          <h2>About the Role</h2>
          <p>We are seeking a Senior Developer with 5+ years of experience in Python and cloud systems. You will lead the architecture of distributed data crawlers, collaborate across engineering teams, and deliver robust API infrastructure.</p>
          <h3>Requirements</h3>
          <p>Proficiency with Python, PostgreSQL, and scalable microservices.</p>
          <a href="#">Apply Now</a>
        </div>
      </body>
    </html>
    """

    with patch.object(extractor, "_fetch_payload", return_value=(sample_html, "text/html")):
        res = extractor.extract(inp, ["https://example.com/job/101"])

        assert res.strategy == "JDRGX"
        assert res.verified is True
        assert len(res.jdrgx_patterns) == 1
        assert res.jdrgx_patterns[0]["pattern"] == r"(?s)<div class=\"job-description\">(.+?)Apply Now"
        assert mock_llm.call.call_count == 2  # Proves 1 self-healing retry succeeded


# ── 4. P0 MOVE_TO_JD Guard Tests ───────────────────────────────────────────────

def test_compiler_move_to_jd_guard_success():
    compiler = Compiler()
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    # 1. MOVE_TO_JD=1 with JDRGX -> Accepted
    loc_res = LOCRGXResult(
        locrgx=r"(?s)<li><a href='([^']+)'>([^<]+)",
        locrgxseq="JOBLINK,JOBTITLE",
        move_to_jd=1,
        jdrgx=r"(?s)<div class=\"desc\">(.+?)</div>",
        jdrgxseq="JOBDESC"
    )
    cfg1 = compiler.from_locrgx(inp, loc_res)
    assert cfg1.body["MOVE_TO_JD"] == 1
    assert "JDRGX1" in cfg1.body

    # 2. MOVE_TO_JD=1 with JDJSON -> Accepted
    llm_res = LLMExtractionResult(
        api_url="https://api.example.com/jobs",
        field_jobtitle="title",
        field_jobid="id",
        field_joblink="id",
        move_to_jd=1,
        jdjson_patterns=[{"path": "data,desc", "seq": "JOBDESC"}]
    )
    cfg2 = compiler.from_llm(inp, llm_res)
    assert cfg2.body["MOVE_TO_JD"] == 1
    assert "JDJSON1" in cfg2.body

    # 3. MOVE_TO_JD=0 -> Accepted (no detail pattern required)
    loc_res_zero = LOCRGXResult(
        locrgx=r"(?s)<li><a href='([^']+)'>([^<]+)",
        locrgxseq="JOBLINK,JOBTITLE",
        move_to_jd=0
    )
    cfg3 = compiler.from_locrgx(inp, loc_res_zero)
    assert cfg3.body["MOVE_TO_JD"] == 0
    assert not any(k.startswith("JDRGX") for k in cfg3.body)


def test_compiler_move_to_jd_guard_rejection():
    compiler = Compiler()
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    # 1. LOCRGX with MOVE_TO_JD=1 but neither JDRGX nor JDJSON -> Raises ValueError
    loc_res_broken = LOCRGXResult(
        locrgx=r"(?s)<li><a href='([^']+)'>([^<]+)",
        locrgxseq="JOBLINK,JOBTITLE",
        move_to_jd=1,
        jdrgx=None,
        jdrgx_patterns=[],
        jdjson_patterns=[]
    )
    with pytest.raises(ValueError, match="MOVE_TO_JD=1 requires at least one verified JDRGX or JDJSON"):
        compiler.from_locrgx(inp, loc_res_broken)

    # 2. LLM with MOVE_TO_JD=1 but neither JDRGX nor JDJSON -> Raises ValueError
    llm_res_broken = LLMExtractionResult(
        api_url="https://api.example.com/jobs",
        field_jobtitle="title",
        field_jobid="id",
        field_joblink="id",
        move_to_jd=1,
        jdrgx_patterns=[],
        jdjson_patterns=[]
    )
    with pytest.raises(ValueError, match="MOVE_TO_JD=1 requires at least one verified JDRGX or JDJSON"):
        compiler.from_llm(inp, llm_res_broken)


def test_compile_step_move_to_jd_guard():
    from src.compile_step import ConfigCompileStep
    from src.pipeline_step import PipelineState, StepSignal
    from src.models import TechStatus

    compile_step = ConfigCompileStep()
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    # Broken LOCRGX state with MOVE_TO_JD=1 and no detail patterns
    from src.pipeline_step import GeneratorOutput
    state = PipelineState(output=GeneratorOutput(input=inp))
    state.detection_path = "locrgx"
    state.locrgx_result = LOCRGXResult(
        locrgx=r"(?s)<li><a href='([^']+)'>([^<]+)",
        locrgxseq="JOBLINK,JOBTITLE",
        move_to_jd=1,
        jdrgx=None,
        jdrgx_patterns=[],
        jdjson_patterns=[]
    )

    res = compile_step.execute(inp, state)
    assert res.signal == StepSignal.HALT_FAIL
    assert res.reason == "invalid-config-missing-jd-extractor"
    assert state.output.tech_status == TechStatus.FAILED
    assert "MOVE_TO_JD=1 requires at least one verified" in state.output.tech_comments


# ── 5. P1 Multi-Sample Validation Tests ─────────────────────────────────────────

def test_multi_sample_validation_all_pass():
    mock_llm = MagicMock()
    mock_llm.call.return_value = json.dumps({
        "strategy": "JDRGX",
        "patterns": [{"pattern": r"(?s)<div class=\"job-desc\">(.+?)Apply Now", "seq": "JOBDESC"}]
    })
    extractor = JDExtractor(llm_client=mock_llm)
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    html_p1 = """
    <html><body><div class="job-desc">
      <h2>Lead Python Architect</h2>
      <p>Responsibilities: Design and implement high performance backend systems, microservices, and distributed web crawlers. Collaborate with product managers and cross-functional engineering teams.</p>
      <h3>Qualifications</h3>
      <p>Requirements: 8+ years experience in Python, PostgreSQL, and cloud architecture.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """
    html_p2 = """
    <html><body><div class="job-desc">
      <h2>Senior React Engineer</h2>
      <p>Responsibilities: Build responsive, modern frontend interfaces and web apps. Collaborate with UI/UX designers and deliver robust client-side state management.</p>
      <h3>Qualifications</h3>
      <p>Requirements: 5+ years experience in TypeScript, React, Next.js, and CSS.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """
    html_p3 = """
    <html><body><div class="job-desc">
      <h2>DevOps Engineer</h2>
      <p>Responsibilities: Maintain Kubernetes clusters, CI/CD pipelines, and cloud infrastructure. Monitor system reliability and optimize server performance.</p>
      <h3>Qualifications</h3>
      <p>Requirements: 4+ years experience in Docker, Terraform, and AWS infrastructure.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """

    def mock_fetch(url, *args, **kwargs):
        mapping = {
            "https://example.com/job/1": (html_p1, "text/html"),
            "https://example.com/job/2": (html_p2, "text/html"),
            "https://example.com/job/3": (html_p3, "text/html"),
        }
        return mapping.get(url, (None, None))

    with patch.object(extractor, "_fetch_payload", side_effect=mock_fetch):
        res = extractor.extract(inp, ["https://example.com/job/1", "https://example.com/job/2", "https://example.com/job/3"])
        assert res.strategy == "JDRGX"
        assert res.verified is True
        assert res.confidence == 0.95  # 3/3 passed -> Strong confidence


def test_multi_sample_validation_single_sample():
    mock_llm = MagicMock()
    mock_llm.call.return_value = json.dumps({
        "strategy": "JDRGX",
        "patterns": [{"pattern": r"(?s)<div class=\"job-desc\">(.+?)Apply Now", "seq": "JOBDESC"}]
    })
    extractor = JDExtractor(llm_client=mock_llm)
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    html_p1 = """
    <html><body><div class="job-desc">
      <h2>Lead Python Architect</h2>
      <p>Responsibilities: Design and implement high performance backend systems and distributed web crawlers. Collaborate with engineering teams.</p>
      <h3>Qualifications</h3>
      <p>Requirements: 8+ years experience in Python, PostgreSQL, and cloud architecture.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """

    with patch.object(extractor, "_fetch_payload", return_value=(html_p1, "text/html")):
        res = extractor.extract(inp, ["https://example.com/job/1"])
        assert res.strategy == "JDRGX"
        assert res.verified is True
        assert res.confidence == 0.75  # Single sample only


def test_multi_sample_validation_overfit_retry_succeeds():
    mock_llm = MagicMock()
    # Call 1 returns overfitted regex referencing job-101 id
    # Call 2 (retry) returns generalized regex
    mock_llm.call.side_effect = [
        json.dumps({
            "strategy": "JDRGX",
            "patterns": [{"pattern": r"(?s)<div id=\"job-101\" class=\"job-desc\">(.+?)Apply Now", "seq": "JOBDESC"}]
        }),
        json.dumps({
            "strategy": "JDRGX",
            "patterns": [{"pattern": r"(?s)<div[^>]*class=\"job-desc\">(.+?)Apply Now", "seq": "JOBDESC"}]
        })
    ]
    extractor = JDExtractor(llm_client=mock_llm)
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    html_p1 = """
    <html><body><div id="job-101" class="job-desc">
      <h2>Python Dev</h2>
      <p>Responsibilities: Build robust APIs and web scrapers. Requirements include Python, SQL, and Git.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """
    html_p2 = """
    <html><body><div id="job-102" class="job-desc">
      <h2>React Dev</h2>
      <p>Responsibilities: Build modern user interfaces. Requirements include React, TypeScript, and Redux.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """

    def mock_fetch(url, *args, **kwargs):
        mapping = {
            "https://example.com/job/101": (html_p1, "text/html"),
            "https://example.com/job/102": (html_p2, "text/html"),
        }
        return mapping.get(url, (None, None))

    with patch.object(extractor, "_fetch_payload", side_effect=mock_fetch):
        res = extractor.extract(inp, ["https://example.com/job/101", "https://example.com/job/102"])
        assert res.strategy == "JDRGX"
        assert res.verified is True
        assert res.confidence == 0.95
        assert mock_llm.call.call_count == 2  # Overfitting on P2 triggered retry, retry succeeded on both


def test_multi_sample_validation_secondary_fails_materially():
    mock_llm = MagicMock()
    # Both initial and retry attempts return overfitted regex referencing job-101
    mock_llm.call.return_value = json.dumps({
        "strategy": "JDRGX",
        "patterns": [{"pattern": r"(?s)<div id=\"job-101\" class=\"job-desc\">(.+?)Apply Now", "seq": "JOBDESC"}]
    })
    extractor = JDExtractor(llm_client=mock_llm)
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    html_p1 = """
    <html><body><div id="job-101" class="job-desc">
      <h2>Python Dev</h2>
      <p>Responsibilities: Build robust APIs and web scrapers. Requirements include Python, SQL, and Git.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """
    html_p2 = """
    <html><body><div id="job-102" class="job-desc">
      <h2>React Dev</h2>
      <p>Responsibilities: Build modern user interfaces. Requirements include React, TypeScript, and Redux.</p>
      <a href="/apply">Apply Now</a>
    </div></body></html>
    """

    def mock_fetch(url, *args, **kwargs):
        mapping = {
            "https://example.com/job/101": (html_p1, "text/html"),
            "https://example.com/job/102": (html_p2, "text/html"),
        }
        return mapping.get(url, (None, None))

    with patch.object(extractor, "_fetch_payload", side_effect=mock_fetch):
        res = extractor.extract(inp, ["https://example.com/job/101", "https://example.com/job/102"])
        assert res.verified is False
        assert "Cross-validation generalization failed" in res.error_msg


# ── 6. P1 Embedded JSON Routing Tests ──────────────────────────────────────────

def test_embedded_json_routes_to_jdrgx_and_extracts():
    """
    HTML pages containing <script type="application/ld+json"> must be handled
    via JDRGX (regex on HTML), not direct JDJSON, ensuring compatibility with JPERL.
    """
    mock_llm = MagicMock()
    mock_llm.call.return_value = json.dumps({
        "strategy": "JDRGX",
        "patterns": [{"pattern": r'(?s)<script[^>]*type="application/ld\+json"[^>]*>.*?"description":\s*"([^"]+)"', "seq": "JOBDESC"}]
    })
    extractor = JDExtractor(llm_client=mock_llm)
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    html_with_ldjson = """
    <!DOCTYPE html>
    <html>
    <head>
      <title>Senior Software Engineer</title>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Senior Software Engineer",
        "description": "Responsibilities: Develop distributed scrapers, microservices, and backend APIs. Requirements: 5+ years experience in Python and PostgreSQL."
      }
      </script>
    </head>
    <body>
      <div><h1>Careers at TechCorp</h1></div>
    </body>
    </html>
    """

    with patch.object(extractor, "_fetch_payload", return_value=(html_with_ldjson, "text/html")):
        res = extractor.extract(inp, ["https://example.com/job/101"])
        assert res.strategy == "JDRGX"
        assert res.verified is True
        assert "JDRGX" == res.strategy
        assert len(res.jdrgx_patterns) == 1
        assert "Responsibilities: Develop distributed scrapers" in res.sample_extracted.get("JOBDESC", "")


# ── 7. P1 Structured JSON Subtree Extraction Tests ─────────────────────────────

def test_jd_detector_json_structured_windowing():
    detector = JDDetector.get_instance()

    # Create a 60KB JSON with massive unrelated metadata array + deeply nested JD field
    large_unrelated_list = [{"log_id": i, "meta": "x" * 200} for i in range(250)]
    deep_data = {
        "status": 200,
        "logs": large_unrelated_list,
        "result": {
            "metadata": {"server": "us-east-1", "timestamp": 1724000000},
            "jobPosting": {
                "id": "REQ-9999",
                "title": "Staff Backend Engineer",
                "jobDescription": "Responsibilities: Architect high throughput data pipelines and distributed services. Requirements: 10+ years backend experience in Python and Go.",
                "qualifications": "Strong systems design background."
            }
        }
    }
    raw_json = json.dumps(deep_data)
    assert len(raw_json) > 50_000

    windowed_json, meta = detector.distill_and_window_json(raw_json, max_chars=15_000)
    assert len(windowed_json) <= 15_000
    assert meta["strategy"] == "structured_subtree_pruning"

    # Verify that the pruned JSON is valid JSON
    parsed_pruned = json.loads(windowed_json)
    assert "result" in parsed_pruned
    assert "jobPosting" in parsed_pruned["result"]
    assert "jobDescription" in parsed_pruned["result"]["jobPosting"]
    assert "Architect high throughput data pipelines" in parsed_pruned["result"]["jobPosting"]["jobDescription"]
    # Verify that the massive list was summarized
    assert len(parsed_pruned["logs"]) <= 3


def test_jd_extractor_large_json_subtree_extraction():
    detector = JDDetector.get_instance()
    mock_llm = MagicMock()
    mock_llm.call.return_value = json.dumps({
        "strategy": "JDJSON",
        "patterns": [{"path": "result,jobPosting,jobDescription", "seq": "JOBDESC"}]
    })
    extractor = JDExtractor(llm_client=mock_llm)
    inp = GeneratorInput(site_id="12345", crawler_id="12345", company_name="Example", career_site_url="https://example.com/careers")

    # 80KB JSON payload with jobDescription at the end
    large_unrelated_list = [{"id": i, "text": "filler " * 50} for i in range(200)]
    payload_obj = {
        "analytics": large_unrelated_list,
        "result": {
            "jobPosting": {
                "id": "101",
                "title": "Principal Architect",
                "jobDescription": "Responsibilities: Lead the design of fault-tolerant crawler infrastructure and cloud microservices. Requirements: 10+ years engineering experience with Python, Go, and Kafka."
            }
        }
    }
    raw_json_str = json.dumps(payload_obj)
    assert len(raw_json_str) > 70_000

    with patch.object(extractor, "_fetch_payload", return_value=(raw_json_str, "application/json")):
        res = extractor.extract(inp, ["https://example.com/api/job/101"])
        assert res.strategy == "JDJSON"
        assert res.verified is True
        assert res.jdjson_patterns[0]["path"] == "result,jobPosting,jobDescription"
        # Proves path was validated on FULL raw JSON
        assert "Lead the design of fault-tolerant crawler infrastructure" in res.sample_extracted.get("JOBDESC", "")


# ── 8. P2 Regex Compatibility Sanitizer Tests ──────────────────────────────────

def test_jdrgx_regex_compatibility_sanitizer():
    # 1. Python named capture group (?P<desc>...) converted to indexed group (...)
    raw_pat1 = r'(?s)<div class="jd">(?P<desc>.+?)</div>'
    sanitized1 = JDExtractor._sanitize_jdrgx_compatibility(raw_pat1)
    assert sanitized1 == r'(?s)<div class="jd">(.+?)</div>'

    # 2. Python-only flags (?u), (?a) stripped
    raw_pat2 = r'(?u)(?s)<div class="jd">(.+?)</div>'
    sanitized2 = JDExtractor._sanitize_jdrgx_compatibility(raw_pat2)
    assert sanitized2 == r'(?s)<div class="jd">(.+?)</div>'

    # 3. Standard JPERL flags preserved
    raw_pat3 = r'(?s)<article id="job-body">(.+?)</article>'
    sanitized3 = JDExtractor._sanitize_jdrgx_compatibility(raw_pat3)
    assert sanitized3 == raw_pat3





