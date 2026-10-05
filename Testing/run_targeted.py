import csv
import os
import sys
import time
import sqlite3
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set environment variables from .env
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                val_str = _val.strip()
                if val_str.startswith('"') and val_str.endswith('"'):
                    val_str = val_str[1:-1]
                elif val_str.startswith("'") and val_str.endswith("'"):
                    val_str = val_str[1:-1]
                os.environ[_key.strip()] = val_str

from src.main import ConfigGenerator
from src.models import GeneratorInput
from src.run_logger import RunLogger

TARGET_IDS = set(sys.argv[1:]) if len(sys.argv) > 1 else {"2701556", "124010646", "3620202", "1881218", "3832820", "3596758"}
INPUT_CSV = "Testing/input/input_records.csv"
TRUTH_CSV = "Testing/ground_truth.csv"
OUTPUT_CSV = "Testing/output/output_results.csv"
REPLAY_LOG_JSON = "Testing/output/replay_log.json"

OUTPUT_COLS = [
    "crawlerId", "companyName", "siteId", "careerSiteUrl",
    "techStatus", "subTechComment", "techComments",
    "siteType", "crawlerType", "confidence", "config",
    "jperl_config", "xpath_config", "primary_config_type",
]

COMPARE_COLS = [
    ("techStatus",     "techStatus"),
    ("subTechComment", "subTechComment"),
    ("siteType",       "siteType"),
    ("crawlerType",    "crawlerType"),
]

def clear_target_cache():
    try:
        from urllib.parse import urlparse
        input_rows = load_csv(INPUT_CSV)
        target_urls = [r["careerSiteUrl"] for r in input_rows if r["crawlerId"] in TARGET_IDS]
        domains = set()
        for u in target_urls:
            p = urlparse(u)
            if p.netloc:
                domains.add(p.netloc.lower())
                if p.netloc.lower().startswith("www."):
                    domains.add(p.netloc.lower()[4:])
                else:
                    domains.add(f"www.{p.netloc.lower()}")
        
        conn = sqlite3.connect('knowledge_base/config_cache.db')
        cursor = conn.cursor()
        for domain in domains:
            try:
                cursor.execute("DELETE FROM config_cache WHERE domain = ?", (domain,))
            except Exception:
                pass
            try:
                cursor.execute("DELETE FROM jd_strategy_cache WHERE domain = ?", (domain,))
            except Exception:
                pass
        conn.commit()
        conn.close()
        print(f"Cleared cache for target domains: {domains}")
    except Exception as e:
        print(f"Cache clear note: {e}")

def load_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return list(csv.DictReader(f))

def save_output_results(results: list[dict]):
    try:
        os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_COLS)
            writer.writeheader()
            writer.writerows(results)
    except Exception as e:
        print(f"Warning: failed to write output results to {OUTPUT_CSV}: {e}")

def save_replay_log(replay_logs: list[dict]):
    try:
        os.makedirs(os.path.dirname(REPLAY_LOG_JSON), exist_ok=True)
        with open(REPLAY_LOG_JSON, "w", encoding="utf-8") as f:
            json.dump(replay_logs, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: failed to write replay log: {e}")

def run():
    clear_target_cache()
    
    # Read ground truth
    truth_rows = load_csv(TRUTH_CSV)
    truth_by_id = {r["crawlerId"]: r for r in truth_rows}

    # Read input records
    input_rows = load_csv(INPUT_CSV)
    target_records = [r for r in input_rows if r["crawlerId"] in TARGET_IDS]
    
    print(f"Running pipeline on {len(target_records)} target records...")
    
    # Load existing output results
    existing_results = load_csv(OUTPUT_CSV)
    results_by_id = {r["crawlerId"]: r for r in existing_results}
    
    # Load existing replay logs
    existing_replay_logs = []
    if os.path.exists(REPLAY_LOG_JSON):
        try:
            with open(REPLAY_LOG_JSON, encoding="utf-8") as f:
                existing_replay_logs = json.load(f)
        except Exception:
            pass
    replay_logs_by_site_id = {item["site_id"]: item for item in existing_replay_logs if "site_id" in item}

    generator = ConfigGenerator()
    run_logger = RunLogger()
    
    for idx, row in enumerate(target_records, 1):
        crawler_id = row["crawlerId"]
        company = row["companyName"]
        url = row["careerSiteUrl"]
        site_id = row["siteId"]
        
        print(f"\n[{idx}/{len(target_records)}] Running: {company} -> {url[:60]}")
        
        truth_row = truth_by_id.get(crawler_id) or {}
        il = row.get("integrationLink") or truth_row.get("integrationLink") or None

        inp = GeneratorInput(
            crawler_id=crawler_id,
            company_name=company,
            site_id=site_id,
            career_site_url=url,
            jobs_on_career_page=int(row.get("jobsOnCareerPage") or 0),
            integration_link=il,
        )
        
        t0 = time.time()
        error = None
        
        try:
            output = generator.generate(inp)
            elapsed = time.time() - t0
            cfg_json = json.dumps(output.config.to_json_dict(), ensure_ascii=False) if output.config else ""
            
            tech_status_val = output.tech_status.value if output.tech_status else ""
            sub_comment_val = output.sub_tech_comment.value if output.sub_tech_comment else ""
            site_type_val = output.site_type.value if output.site_type else ""
            crawler_type_val = output.crawler_type.value if output.crawler_type else ""
            
            res_dict = {
                "crawlerId": crawler_id,
                "companyName": company,
                "siteId": site_id,
                "careerSiteUrl": url,
                "techStatus": tech_status_val,
                "subTechComment": sub_comment_val,
                "techComments": output.tech_comments or "",
                "siteType": site_type_val,
                "crawlerType": crawler_type_val,
                "confidence": str(round(output.confidence, 3)),
                "config": cfg_json,
                "jperl_config": json.dumps(output.jperl_config.to_json_dict(), ensure_ascii=False) if output.jperl_config else "",
                "xpath_config": json.dumps(output.xpath_config.to_json_dict(), ensure_ascii=False) if output.xpath_config else "",
                "primary_config_type": output.primary_config_type or "",
            }
            results_by_id[crawler_id] = res_dict
            
            new_log_entry = {
                "site_id": site_id,
                "company_name": company,
                "career_url": url,
                "tech_status": tech_status_val,
                "crawler_type": crawler_type_val or None,
                "config": output.config.body if output.config else None,
                "extracted_jobs": output.extracted_jobs or [],
                "replay_status": output.replay_status or ("PASSED" if tech_status_val == "Done" else "FAILED"),
                "replay_error": output.replay_error or (output.tech_comments if tech_status_val == "Failed" else None)
            }
            replay_logs_by_site_id[site_id] = new_log_entry
            
            # Save intermediate updates
            ordered_results = [results_by_id[r["crawlerId"]] for r in input_rows if r["crawlerId"] in results_by_id]
            ordered_replay = [replay_logs_by_site_id[r["siteId"]] for r in input_rows if r["siteId"] in replay_logs_by_site_id]
            save_output_results(ordered_results)
            save_replay_log(ordered_replay)
            
            print(f"  Status={tech_status_val}  type={crawler_type_val}  conf={output.confidence:.2f}  ({elapsed:.1f}s)")
            
            # Infer detection path
            if not cfg_json:
                det_path = "robot" if tech_status_val == "Non-Workable" else "failed"
            elif site_type_val == "SRP":
                det_path = "srp"
            elif output.confidence == 0.95:
                det_path = "ats"
            else:
                det_path = "llm"
                
            run_logger.log_site(
                crawler_id=crawler_id, company_name=company,
                career_url=url, tech_status=tech_status_val,
                detection_path=det_path, crawler_type=crawler_type_val,
                confidence=output.confidence, elapsed_s=elapsed,
                total_tokens=output.total_tokens, tokens_per_job=output.tokens_per_job,
            )
            
        except Exception as exc:
            elapsed = time.time() - t0
            error = str(exc)
            print(f"  ERROR: {exc}")
            
            res_dict = {
                "crawlerId": crawler_id,
                "companyName": company,
                "siteId": site_id,
                "careerSiteUrl": url,
                "techStatus": "Not Fixable",
                "subTechComment": "",
                "techComments": str(exc),
                "siteType": "",
                "crawlerType": "",
                "confidence": "0",
                "config": "",
                "jperl_config": "",
                "xpath_config": "",
                "primary_config_type": "",
            }
            results_by_id[crawler_id] = res_dict
            
            new_log_entry = {
                "site_id": site_id,
                "company_name": company,
                "career_url": url,
                "tech_status": "Failed",
                "crawler_type": None,
                "config": None,
                "extracted_jobs": [],
                "replay_status": "FAILED",
                "replay_error": str(exc)
            }
            replay_logs_by_site_id[site_id] = new_log_entry
            
            # Save intermediate updates
            ordered_results = [results_by_id[r["crawlerId"]] for r in input_rows if r["crawlerId"] in results_by_id]
            ordered_replay = [replay_logs_by_site_id[r["siteId"]] for r in input_rows if r["siteId"] in replay_logs_by_site_id]
            save_output_results(ordered_results)
            save_replay_log(ordered_replay)
            
            from src.llm_client import LLMClient
            tot_tok = LLMClient.total_prompt_tokens + LLMClient.total_completion_tokens
            total_jobs = int(row["jobsOnCareerPage"]) if row.get("jobsOnCareerPage") else 0
            tok_per_j = tot_tok / total_jobs if total_jobs > 0 else 0.0
            
            run_logger.log_site(
                crawler_id=crawler_id, company_name=company,
                career_url=url, tech_status="Not Fixable",
                detection_path="error", crawler_type="", confidence=0.0,
                elapsed_s=elapsed, error=error,
                total_tokens=tot_tok, tokens_per_job=tok_per_j,
            )
            
        if idx < len(target_records):
            print("Sleeping 10 seconds to avoid Gemini rate limits...")
            time.sleep(10.0)

    generator.close()
    
    # Save final results and replay logs
    ordered_results = [results_by_id[r["crawlerId"]] for r in input_rows if r["crawlerId"] in results_by_id]
    ordered_replay = [replay_logs_by_site_id[r["siteId"]] for r in input_rows if r["siteId"] in replay_logs_by_site_id]
    save_output_results(ordered_results)
    save_replay_log(ordered_replay)
    print(f"\n[OK] Results -> {OUTPUT_CSV}")
    print(f"[OK] Replay log -> {REPLAY_LOG_JSON}")

    # Finish run log
    log_path = run_logger.finish()
    print(f"[OK] Run log -> {log_path}")

    # Comparison report
    print("\n" + "="*80)
    print("COMPARISON: Pipeline Output vs Ground Truth")
    print("="*80)

    matches = {col: 0 for col, _ in COMPARE_COLS}
    totals = {col: 0 for col, _ in COMPARE_COLS}

    for crawler_id in TARGET_IDS:
        res = results_by_id.get(crawler_id)
        if not res:
            continue
        truth = truth_by_id.get(crawler_id, {})
        print(f"\n  Company : {res['companyName']}")
        print(f"  URL     : {res['careerSiteUrl'][:70]}")
        
        for key, truth_key in COMPARE_COLS:
            got = res.get(key, "")
            exp = truth.get(truth_key, "")
            totals[key] += 1
            if got == exp:
                matches[key] += 1
                print(f"    [MATCH] {key:<16} got='{got}'")
            else:
                print(f"    [DIFF]  {key:<16} got='{got}' expected='{exp}'")

    print("\n" + "-"*80)
    print("ACCURACY SUMMARY")
    print("-"*80)
    acc_sum = 0.0
    for col, _ in COMPARE_COLS:
        m = matches[col]
        t = totals[col]
        pct = (m / t * 100) if t > 0 else 0
        acc_sum += pct
        print(f"  {col:<20}: {m}/{t}  ({pct:.0f}%)")

    overall = acc_sum / len(COMPARE_COLS) if len(COMPARE_COLS) > 0 else 0.0
    print(f"\n  Overall accuracy: {overall:.0f}%")

if __name__ == "__main__":
    run()
