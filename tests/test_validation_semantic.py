import pytest
from src.validation import validate_job_objects, normalize_source_text
from src.extraction.regex_parser import RegexParser
from src.extraction.json_parser import JsonParser


def test_normalize_source_text():
    # 1. HTML entities
    raw_html = '{"id": &quot;120751000009423232&quot;, "title": &#34;Software Engineer&#34;}'
    norm = normalize_source_text(raw_html)
    assert '120751000009423232' in norm
    assert 'software engineer' in norm

    # 2. Hex and Unicode escapes
    raw_escapes = r'{"id": \x22REQ-990\x22, "title": \u0022Python Developer\u0022}'
    norm_escapes = normalize_source_text(raw_escapes)
    assert 'req-990' in norm_escapes
    assert 'python developer' in norm_escapes

    # 3. URL encoded
    raw_url = 'title=Senior%20QA%20Engineer&location=New%20York'
    norm_url = normalize_source_text(raw_url)
    assert 'senior qa engineer' in norm_url
    assert 'new york' in norm_url


def test_valid_jobid_with_html_entities_and_escapes():
    raw_html = """
    <div class="job-item">
      <input type="hidden" value="[{&quot;id&quot;:&quot;120751000009423232&quot;,&quot;title&quot;:&quot;Python Developer&quot;}]">
    </div>
    """
    job_objects = [
        {
            "JOBTITLE": "Python Developer",
            "JOBID": "120751000009423232",
            "JOBLINK": "https://example.com/jobs/120751000009423232",
            "LOCATION": "Remote"
        }
    ]
    raw_html_with_loc = raw_html + "<span>Remote</span>"
    ok, err = validate_job_objects(job_objects, expected_count=1, raw_source=raw_html_with_loc)
    assert ok is True, f"Validation should pass for HTML-entity encoded JOBID: {err}"


def test_hallucinated_jobid_rejected():
    raw_html = """
    <div class="job-item">
      <h2>Senior Python Engineer</h2>
      <p>Location: Bangalore</p>
    </div>
    """
    job_objects = [
        {
            "JOBTITLE": "Senior Python Engineer",
            "JOBID": "FABRICATED-ID-99999",
            "LOCATION": "Bangalore"
        }
    ]
    ok, err = validate_job_objects(job_objects, expected_count=1, raw_source=raw_html)
    assert ok is False
    assert "hallucination detected" in err or "not found in normalized raw source" in err


def test_ui_noise_jobid_rejected():
    raw_html = """
    <div class="job-item" id="item-container-1">
      <a onclick="javascript:apply(1)">Java Architect</a>
    </div>
    """
    # 1. Javascript click handler
    job_objects_js = [
        {
            "JOBTITLE": "Java Architect",
            "JOBID": "javascript:apply(1)"
        }
    ]
    ok, err = validate_job_objects(job_objects_js, expected_count=1, raw_source=raw_html)
    assert ok is False
    assert "invalid UI or JS noise" in err

    # 2. UI container class / accordion ID
    job_objects_tab = [
        {
            "JOBTITLE": "Java Architect",
            "JOBID": "tab-1"
        }
    ]
    ok, err = validate_job_objects(job_objects_tab, expected_count=1, raw_source=raw_html)
    assert ok is False
    assert "invalid UI or JS noise" in err


def test_identical_static_jobid_rejected():
    raw_html = """
    <div class="cs2-item"><h2>Dev 1</h2></div>
    <div class="cs2-item"><h2>Dev 2</h2></div>
    """
    # If the LLM captured the CSS class name as JOBID for all jobs
    job_objects = [
        {"JOBTITLE": "Dev 1", "JOBID": "cs2-item"},
        {"JOBTITLE": "Dev 2", "JOBID": "cs2-item"}
    ]
    ok, err = validate_job_objects(job_objects, expected_count=2, raw_source=raw_html)
    assert ok is False
    assert "identical non-title JOBID" in err


def test_duplicate_jobtitle_across_different_locations():
    # Two distinct job openings with the same title in different locations
    raw_html = """
    <div class="card"><h3>Software Engineer</h3><span class="city">Bangalore</span></div>
    <div class="card"><h3>Software Engineer</h3><span class="city">Pune</span></div>
    <div class="card"><h3>Product Manager</h3><span class="city">Mumbai</span></div>
    """
    config = {
        "LOCRGX": r'(?s)<div class="card"><h3>([^<]+)</h3><span class="city">([^<]+)</span></div>',
        "LOCRGXSEQ": "JOBTITLE,LOCATION"
    }
    extracted = RegexParser.execute(config, raw_html)
    assert len(extracted) == 3
    # Verify both Software Engineer records are preserved and disambiguated by their locations
    titles_locations = [(j["JOBTITLE"], j["LOCATION"], j["JOBID"]) for j in extracted]
    assert ("Software Engineer", "Bangalore", "Software Engineer") in titles_locations
    assert ("Software Engineer", "Pune", "Software Engineer") in titles_locations
    assert ("Product Manager", "Mumbai", "Product Manager") in titles_locations

    ok, err = validate_job_objects(extracted, expected_count=3, raw_source=raw_html)
    assert ok is True, f"Validation failed: {err}"


def test_jobtitle_fallback_hierarchy_in_regex_parser():
    raw_html = """
    <div class="job-box">
      <h3>Frontend Developer</h3>
      <a href="/jobs/101">Apply</a>
    </div>
    <div class="job-box">
      <h3>Backend Developer</h3>
      <span>No Link</span>
    </div>
    """
    # Case A: When JOBLINK is extracted, JOBID falls back to JOBLINK
    config_with_link = {
        "LOCRGX": r'(?s)<div class="job-box">\s*<h3>([^<]+)</h3>\s*<a href="([^"]+)">',
        "LOCRGXSEQ": "JOBTITLE,JOBLINK"
    }
    jobs_a = RegexParser.execute(config_with_link, raw_html)
    assert len(jobs_a) == 1
    assert jobs_a[0]["JOBTITLE"] == "Frontend Developer"
    assert jobs_a[0]["JOBLINK"] == "/jobs/101"
    assert jobs_a[0]["JOBID"] == "/jobs/101"

    # Case B: When JOBLINK is absent, JOBID falls back to job-specific JOBTITLE
    config_no_link = {
        "LOCRGX": r'(?s)<div class="job-box">\s*<h3>([^<]+)</h3>',
        "LOCRGXSEQ": "JOBTITLE"
    }
    jobs_b = RegexParser.execute(config_no_link, raw_html)
    assert len(jobs_b) == 2
    assert jobs_b[0]["JOBTITLE"] == "Frontend Developer"
    assert jobs_b[0]["JOBID"] == "Frontend Developer"
    assert jobs_b[1]["JOBTITLE"] == "Backend Developer"
    assert jobs_b[1]["JOBID"] == "Backend Developer"


def test_jobtitle_fallback_hierarchy_in_json_parser():
    raw_json = """
    [
      {"title": "DevOps Engineer", "link": "/jobs/201"},
      {"title": "Site Reliability Engineer"}
    ]
    """
    config = {
        "LOCJSON": "title|X|link",
        "LOCJSONSEQ": "JOBTITLE,JOBLINK"
    }
    jobs = JsonParser.execute(config, raw_json)
    assert len(jobs) == 2
    assert jobs[0]["JOBTITLE"] == "DevOps Engineer"
    assert jobs[0]["JOBLINK"] == "/jobs/201"
    assert jobs[0]["JOBID"] == "/jobs/201"

    assert jobs[1]["JOBTITLE"] == "Site Reliability Engineer"
    assert jobs[1]["JOBLINK"] == ""
    assert jobs[1]["JOBID"] == "Site Reliability Engineer"
