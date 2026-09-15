"""
pipeline_runner.py
──────────────────
Asynchronous pipeline execution adapter for WebSockets and TechOps Dashboard.

Applies the Decorator Pattern (ObservableStep) over standard PipelineSteps
to stream real-time step status, latencies, logs, and token metrics to the UI
without altering core engine logic (adheres to OCP and DRY principles).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlparse

from src.ats_fingerprinter import ATSFingerprinter
from src.compile_step import ConfigCompileStep
from src.config import CACHE_DB_PATH
from src.config_cache import ConfigCacheStep
from src.heuristic_ranker import HeuristicRanker
from src.jd_strategy_discovery import JDStrategyDiscovery
from src.llm_client import LLMClient
from src.llm_reasoner import LLMReasoner
from src.locrgx_generator import LOCRGXGenerator
from src.main import ConfigGenerator
from src.models import (
    CrawlerType,
    GeneratorInput,
    GeneratorOutput,
    SiteType,
    SubTechComment,
    TechStatus,
)
from src.pipeline_step import PipelineState, PipelineStep, StepResult, StepSignal
from src.robot_checker import RobotChecker
from src.source_resolver import SourceResolver
from src.srp_classifier import SRPClassifier
from src.traffic_interceptor import TrafficInterceptor
from src.wp_rest_detector import WPRestDetector
from src.xpath_srp_generator import XPathSRPGenerator

logger = logging.getLogger(__name__)

# Standard 13 Steps in Pipeline Order
STEP_DEFINITIONS = [
    {"id": 1,  "key": "robot_checker",     "name": "RobotChecker",     "short": "RobotChecker",   "pending_desc": "Robots.txt Rule Check",      "active_desc": "Checking robots.txt..."},
    {"id": 2,  "key": "ats_fingerprint",   "name": "ATSFingerprinter", "short": "ATSFingerprint", "pending_desc": "Scan ATS Signatures",        "active_desc": "Scanning 21 ATS Signatures..."},
    {"id": 3,  "key": "config_cache",      "name": "ConfigCacheStep",  "short": "ConfigCache",    "pending_desc": "Domain Cache Check",        "active_desc": "Checking SQLite Cache..."},
    {"id": 4,  "key": "traffic_sniffer",   "name": "TrafficInterceptor","short": "TrafficSniffer", "pending_desc": "Sniff XHR & Network",       "active_desc": "Sniffing Live XHR & DOM..."},
    {"id": 5,  "key": "heuristic_rank",    "name": "HeuristicRanker",  "short": "HeuristicRank",  "pending_desc": "Score API Feeds",           "active_desc": "Ranking API Payloads..."},
    {"id": 6,  "key": "source_resolver",   "name": "SourceResolver",   "short": "SourceResolver", "pending_desc": "Arbitrate Data Source",     "active_desc": "Resolving Feed & DOM..."},
    {"id": 7,  "key": "jd_discovery",      "name": "JDStrategyDiscovery","short": "JDDiscovery",  "pending_desc": "Discover JD Routes",        "active_desc": "Testing Detail Links..."},
    {"id": 8,  "key": "wp_rest_detect",    "name": "WPRestDetector",   "short": "WPRestDetect",   "pending_desc": "WP REST Scan",              "active_desc": "Scanning WP REST API..."},
    {"id": 9,  "key": "srp_classifier",    "name": "SRPClassifier",   "short": "SRPClassifier",  "pending_desc": "Classify SRP Layout",       "active_desc": "Classifying HTML Layout..."},
    {"id": 10, "key": "llm_reasoner",      "name": "LLMReasoner",      "short": "LLMReasoner",    "pending_desc": "Reason JSON Schema",        "active_desc": "Reasoning JSON Paths..."},
    {"id": 11, "key": "locrgx_gen",        "name": "LOCRGXGenerator",  "short": "LOCRGXGen",      "pending_desc": "Synthesize PCRE Regex",     "active_desc": "Generating Regex..."},
    {"id": 12, "key": "xpath_gen",         "name": "XPathSRPGenerator","short": "XPathGen",       "pending_desc": "Synthesize DOM XPath",      "active_desc": "Generating DOM XPath..."},
    {"id": 13, "key": "compile_config",    "name": "ConfigCompileStep","short": "CompileConfig",  "pending_desc": "Compile JPERL Config",      "active_desc": "Compiling JPERL DSL..."},
]


class WebSocketLogHandler(logging.Handler):
    """Captures runtime logs during pipeline execution and buffers them for dispatch."""

    def __init__(self, callback: Callable[[str, str, str], None]) -> None:
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = record.levelname
            timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            self._callback(timestamp, level, msg)
        except Exception:
            self.handleError(record)


class ObservableStep(PipelineStep):
    """
    Decorator pattern wrapping a PipelineStep.
    Emits real-time start, log, and finish notifications over WebSocket callbacks.
    """

    def __init__(
        self,
        inner_step: PipelineStep,
        step_id: int,
        on_step_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self._inner = inner_step
        self._step_id = step_id
        self._on_step_event = on_step_event

    @property
    def name(self) -> str:
        return self._inner.name

    def execute(self, inp: GeneratorInput, state: PipelineState) -> StepResult:
        start_t = time.perf_counter()
        step_def = STEP_DEFINITIONS[self._step_id - 1] if self._step_id <= len(STEP_DEFINITIONS) else {}
        active_desc = step_def.get("active_desc", f"Running {self._inner.name}...")

        # Emit Step Started
        self._on_step_event({
            "type": "step_start",
            "step_id": self._step_id,
            "step_name": self._inner.name,
            "stage_desc": active_desc,
            "timestamp": time.time(),
        })

        try:
            result = self._inner.execute(inp, state)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start_t) * 1000)
            logger.exception("Step %s crashed: %s", self._inner.name, exc)
            self._on_step_event({
                "type": "step_finish",
                "step_id": self._step_id,
                "step_name": self._inner.name,
                "status": "failed",
                "badge": "CRASH",
                "stage_desc": f"Crash: {exc}",
                "duration_ms": duration_ms,
                "reason": str(exc),
            })
            raise

        duration_ms = int((time.perf_counter() - start_t) * 1000)

        # Derive dynamic badge label and technical stage description
        badge, stage_desc = self._derive_badge_and_desc(state, result)
        status = "done"

        if result.signal == StepSignal.HALT_FAIL:
            status = "failed"
        elif result.signal == StepSignal.HALT_OK:
            status = "done"
        elif "skip" in result.reason.lower() or "bypass" in result.reason.lower() or "not json_api" in result.reason.lower():
            status = "skipped"

        self._on_step_event({
            "type": "step_finish",
            "step_id": self._step_id,
            "step_name": self._inner.name,
            "status": status,
            "badge": badge,
            "stage_desc": stage_desc,
            "duration_ms": duration_ms,
            "reason": result.reason,
            "detection_path": state.detection_path,
        })

        return result

    def _derive_badge_and_desc(self, state: PipelineState, result: StepResult) -> tuple[str, str]:
        name = self._inner.name
        if name == "RobotChecker":
            if result.signal == StepSignal.HALT_FAIL:
                return "BLOCKED", "Robots.txt Disallowed"
            return "ALLOW", "Robots.txt Allowed"

        if name == "ATSFingerprinter":
            if state.ats_match and state.ats_match.matched:
                rule = state.ats_match.parent_rule_name or "ATS Match"
                return rule, f"ATS Detected: {rule}"
            if state.ats_candidates:
                return f"{len(state.ats_candidates)} ATS", f"Found {len(state.ats_candidates)} Candidate ATS"
            return "No ATS", "Custom Career Portal"

        if name == "ConfigCacheStep":
            if state.detection_path == "cache":
                return "CACHE HIT", "Config Restored from Cache"
            return "CACHE MISS", "Fresh Run (Cache Miss)"

        if name == "TrafficInterceptor":
            count = len(state.captured)
            return f"{count} Req", f"Captured {count} XHR & DOM Payloads"

        if name == "HeuristicRanker":
            if state.candidates:
                top_score = state.candidates[0].score
                return f"Score {top_score:.1f}", f"Ranked Top {len(state.candidates)} APIs"
            return "0 APIs", "No Job Feed APIs"

        if name == "SourceResolver":
            if state.source_decision:
                src = state.source_decision.source.value
                return src, f"Resolved: {src}"
            return "Resolved", "Source Fact Arbitrated"

        if name == "JDStrategyDiscovery":
            if state.jd_strategy_result:
                strat = state.jd_strategy_result.strategy.value
                return strat, f"Navigation: {strat}"
            return "Schema", "JD Navigation Verified"

        if name == "WPRestDetector":
            if state.detection_path == "wp_rest":
                return "WP MATCH", "WordPress REST API Found"
            return "SKIPPED", "Non-WordPress Portal"

        if name == "SRPClassifier":
            return "SRP Page", "HTML Listing Grid" if state.is_srp else "API Candidate"

        if name == "LLMReasoner":
            if state.llm_result:
                return f"Conf {state.llm_result.confidence:.0%}", f"Mapped JSON Fields ({state.llm_result.confidence:.0%})"
            return "SKIPPED", "Skipped (Static HTML)"

        if name == "LOCRGXGenerator":
            if state.locrgx_result:
                return f"Conf {state.locrgx_result.confidence:.0%}", f"Synthesized PCRE Regex ({state.locrgx_result.confidence:.0%})"
            return "SKIPPED", "Skipped (API Mode)"

        if name == "XPathSRPGenerator":
            if state.xpath_srp_result:
                return f"Conf {state.xpath_srp_result.confidence:.0%}", f"Synthesized DOM XPath ({state.xpath_srp_result.confidence:.0%})"
            return "SKIPPED", "Skipped (Regex Done)"

        if name == "ConfigCompileStep":
            return f"Path: {state.detection_path.upper()}", f"Compiled JPERL Config ({state.detection_path.upper()})"

        return "OK", "Completed"


class PipelineRunner:
    """Orchestrates single-site and batch runs with WebSocket event dispatching."""

    @staticmethod
    def clear_domain_cache(career_url: str) -> None:
        """Purges SQLite cache entries for the given URL's domain."""
        try:
            domain = ConfigCacheStep._get_domain(career_url)
            if not domain:
                return
            conn = sqlite3.connect(CACHE_DB_PATH)
            cur = conn.cursor()
            cur.execute("DELETE FROM config_cache WHERE domain = ? OR domain LIKE ?", (domain, f"%{domain}%"))
            try:
                cur.execute("DELETE FROM jd_strategy_cache WHERE domain = ? OR domain LIKE ?", (domain, f"%{domain}%"))
            except Exception:
                pass
            conn.commit()
            conn.close()
            logger.info("PipelineRunner: Cleared cache for domain '%s'", domain)
        except Exception as exc:
            logger.warning("PipelineRunner: Failed to clear cache for %s: %s", career_url, exc)

    @classmethod
    async def run_single(
        cls,
        inp: GeneratorInput,
        force_refresh: bool,
        event_queue: asyncio.Queue,
    ) -> GeneratorOutput:
        """Runs a single job pipeline in an async worker thread while streaming WebSocket events."""
        if force_refresh:
            cls.clear_domain_cache(inp.career_site_url)

        def sync_event_emitter(event: dict[str, Any]) -> None:
            # Threadsafe put into asyncio queue
            event_queue.put_nowait(event)

        # Log listener
        log_handler = WebSocketLogHandler(
            lambda ts, lvl, msg: sync_event_emitter({
                "type": "log",
                "timestamp": ts,
                "level": lvl,
                "message": msg,
            })
        )
        log_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        def worker() -> GeneratorOutput:
            raw_steps = ConfigGenerator._default_steps()
            observable_steps = [
                ObservableStep(step, idx + 1, sync_event_emitter)
                for idx, step in enumerate(raw_steps)
            ]
            generator = ConfigGenerator(steps=observable_steps)
            try:
                return generator.generate(inp)
            finally:
                generator.close()

        start_time = time.perf_counter()
        sync_event_emitter({
            "type": "pipeline_start",
            "crawler_id": inp.crawler_id,
            "company_name": inp.company_name,
            "career_site_url": inp.career_site_url,
            "force_refresh": force_refresh,
            "total_steps": 13,
            "start_time": time.time(),
        })

        try:
            output = await asyncio.to_thread(worker)
        finally:
            root_logger.removeHandler(log_handler)

        elapsed = round(time.perf_counter() - start_time, 2)

        # Format config dict
        config_dict = output.config.to_json_dict() if output.config else None
        jperl_dict = output.jperl_config.to_json_dict() if output.jperl_config else None
        xpath_dict = output.xpath_config.to_json_dict() if output.xpath_config else None

        # Build telemetry summary payload
        telemetry_payload = {
            "type": "pipeline_complete",
            "tech_status": output.tech_status.value if output.tech_status else "Failed",
            "sub_tech_comment": output.sub_tech_comment.value if output.sub_tech_comment else None,
            "tech_comments": output.tech_comments,
            "site_type": output.site_type.value if output.site_type else None,
            "crawler_type": output.crawler_type.value if output.crawler_type else None,
            "confidence": round(output.confidence, 3),
            "confidence_percent": f"{round(output.confidence * 100, 1)}%",
            "primary_config_type": output.primary_config_type,
            "config": config_dict,
            "jperl_config": jperl_dict,
            "xpath_config": xpath_dict,
            "has_both": bool(jperl_dict and xpath_dict),
            "extracted_jobs": output.extracted_jobs or [],
            "total_extracted": len(output.extracted_jobs) if output.extracted_jobs else 0,
            "total_tokens": output.total_tokens,
            "tokens_per_job": round(output.tokens_per_job, 1),
            "runtime_seconds": elapsed,
        }

        sync_event_emitter(telemetry_payload)
        return output

    @classmethod
    async def run_batch(
        cls,
        csv_text: str,
        force_refresh: bool,
        event_queue: asyncio.Queue,
        cancel_event: asyncio.Event,
    ) -> tuple[list[dict], list[dict]]:
        """Executes batch CSV ingestion sequentially, streaming per-row progress events."""
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        rows = list(reader)
        total = len(rows)

        if total == 0:
            event_queue.put_nowait({
                "type": "batch_error",
                "message": "CSV file contains no data rows.",
            })
            return [], []

        event_queue.put_nowait({
            "type": "batch_start",
            "total_targets": total,
            "start_time": time.time(),
        })

        results: list[dict] = []
        replay_logs: list[dict] = []
        passed_count = 0
        failed_count = 0

        for idx, row in enumerate(rows, 1):
            if cancel_event.is_set():
                event_queue.put_nowait({
                    "type": "batch_cancelled",
                    "completed_count": idx - 1,
                    "total": total,
                })
                break

            crawler_id = row.get("crawlerId", "").strip() or f"crw_{idx}"
            company_name = row.get("companyName", "").strip() or f"Company_{idx}"
            site_id = row.get("siteId", "").strip() or f"{crawler_id}_SRP"
            career_url = row.get("careerSiteUrl", "").strip()
            jobs_on_page = int(row.get("jobsOnCareerPage") or row.get("jobs_on_career_page") or 0)
            integration_link = row.get("integrationLink", "").strip() or None

            if not career_url:
                continue

            event_queue.put_nowait({
                "type": "batch_row_start",
                "row_index": idx,
                "total": total,
                "crawler_id": crawler_id,
                "company_name": company_name,
                "career_site_url": career_url,
            })

            inp = GeneratorInput(
                crawler_id=crawler_id,
                company_name=company_name,
                site_id=site_id,
                career_site_url=career_url,
                jobs_on_career_page=jobs_on_page,
                integration_link=integration_link,
            )

            try:
                output = await cls.run_single(inp, force_refresh, event_queue)
                status_val = output.tech_status.value if output.tech_status else "Failed"
                sub_val = output.sub_tech_comment.value if output.sub_tech_comment else ""

                if output.tech_status == TechStatus.DONE:
                    passed_count += 1
                else:
                    failed_count += 1

                res_entry = {
                    "crawlerId": crawler_id,
                    "companyName": company_name,
                    "siteId": site_id,
                    "careerSiteUrl": career_url,
                    "techStatus": status_val,
                    "subTechComment": sub_val,
                    "techComments": output.tech_comments or "",
                    "siteType": output.site_type.value if output.site_type else "",
                    "crawlerType": output.crawler_type.value if output.crawler_type else "",
                    "confidence": round(output.confidence, 3),
                    "config": json.dumps(output.config.to_json_dict(), ensure_ascii=False) if output.config else "",
                    "jperl_config": json.dumps(output.jperl_config.to_json_dict(), ensure_ascii=False) if output.jperl_config else "",
                    "xpath_config": json.dumps(output.xpath_config.to_json_dict(), ensure_ascii=False) if output.xpath_config else "",
                    "primary_config_type": output.primary_config_type or "",
                }
                results.append(res_entry)

                if output.extracted_jobs:
                    replay_logs.append({
                        "crawlerId": crawler_id,
                        "siteId": site_id,
                        "extracted_count": len(output.extracted_jobs),
                        "jobs": output.extracted_jobs,
                    })

                event_queue.put_nowait({
                    "type": "batch_row_complete",
                    "row_index": idx,
                    "total": total,
                    "passed_count": passed_count,
                    "failed_count": failed_count,
                    "progress_percent": round((idx / total) * 100, 1),
                    "result": res_entry,
                })

            except Exception as exc:
                failed_count += 1
                err_entry = {
                    "crawlerId": crawler_id,
                    "companyName": company_name,
                    "siteId": site_id,
                    "careerSiteUrl": career_url,
                    "techStatus": "Failed",
                    "subTechComment": "Exception",
                    "techComments": str(exc),
                    "siteType": "",
                    "crawlerType": "",
                    "confidence": 0.0,
                    "config": "",
                    "jperl_config": "",
                    "xpath_config": "",
                    "primary_config_type": "",
                }
                results.append(err_entry)
                event_queue.put_nowait({
                    "type": "batch_row_complete",
                    "row_index": idx,
                    "total": total,
                    "passed_count": passed_count,
                    "failed_count": failed_count,
                    "progress_percent": round((idx / total) * 100, 1),
                    "result": err_entry,
                })

        event_queue.put_nowait({
            "type": "batch_complete",
            "total_processed": len(results),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "success_rate": f"{round((passed_count / max(1, len(results))) * 100, 1)}%",
        })

        return results, replay_logs
