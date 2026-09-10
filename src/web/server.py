"""
server.py
─────────
FastAPI web application and WebSocket server for AI-Config-Engine TechOps Dashboard.

Provides:
  - Static file hosting for frontend/
  - Real-time bidirectional WebSocket pipeline execution (/ws/pipeline)
  - Batch CSV export and preview REST endpoints
  - Multi-provider gateway telemetry healthcheck
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from src.config import CACHE_DB_PATH, LLM_PROVIDER
from src.models import GeneratorInput
from src.web.pipeline_runner import PipelineRunner

logger = logging.getLogger(__name__)

# Base Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
OUTPUT_DIR = os.path.join(BASE_DIR, "Testing", "output")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(FRONTEND_DIR, exist_ok=True)

app = FastAPI(
    title="AI-Config-Engine TechOps Console",
    version="2.4.0",
    description="Real-time web crawling config synthesizer & validation engine",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global in-memory storage for batch exports
_LATEST_RESULTS_CSV: Optional[str] = None
_LATEST_REPLAY_LOGS: Optional[list[dict]] = None
_ACTIVE_CANCEL_EVENT: Optional[asyncio.Event] = None


@app.get("/", response_class=HTMLResponse)
async def serve_root():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>AI-Config-Engine TechOps Dashboard</h1><p>Frontend index.html not found.</p>")


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "provider": LLM_PROVIDER,
        "version": "2.4.0",
        "cache_db": os.path.exists(CACHE_DB_PATH),
        "frontend_ready": os.path.exists(os.path.join(FRONTEND_DIR, "index.html")),
    }


@app.post("/api/batch/preview")
async def preview_batch_csv(file: UploadFile = File(...)):
    try:
        content = await file.read()
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text.strip()))
        rows = list(reader)
        preview_rows = rows[:5]
        return {
            "success": True,
            "filename": file.filename,
            "total_rows": len(rows),
            "size_bytes": len(content),
            "preview": preview_rows,
            "raw_text": text,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse CSV: {exc}")


@app.get("/api/batch/download/results")
async def download_results_csv():
    global _LATEST_RESULTS_CSV
    csv_path = os.path.join(OUTPUT_DIR, "output_results.csv")
    if _LATEST_RESULTS_CSV:
        return Response(
            content=_LATEST_RESULTS_CSV,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=output_results.csv"},
        )
    if os.path.exists(csv_path):
        return FileResponse(
            csv_path,
            media_type="text/csv",
            filename="output_results.csv",
        )
    raise HTTPException(status_code=404, detail="No batch results available for download.")


@app.get("/api/batch/download/replay")
async def download_replay_log():
    global _LATEST_REPLAY_LOGS
    log_path = os.path.join(OUTPUT_DIR, "replay_log.json")
    if _LATEST_REPLAY_LOGS is not None:
        return JSONResponse(
            content=_LATEST_REPLAY_LOGS,
            headers={"Content-Disposition": "attachment; filename=replay_log.json"},
        )
    if os.path.exists(log_path):
        return FileResponse(
            log_path,
            media_type="application/json",
            filename="replay_log.json",
        )
    raise HTTPException(status_code=404, detail="No replay logs available for download.")


@app.websocket("/ws/pipeline")
async def websocket_pipeline_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket client connected to /ws/pipeline")

    event_queue: asyncio.Queue = asyncio.Queue()
    cancel_event: asyncio.Event = asyncio.Event()
    global _ACTIVE_CANCEL_EVENT, _LATEST_RESULTS_CSV, _LATEST_REPLAY_LOGS
    _ACTIVE_CANCEL_EVENT = cancel_event

    async def event_pump():
        try:
            while True:
                event = await event_queue.get()
                await websocket.send_json(event)
                event_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Event pump terminated: %s", exc)

    pump_task = asyncio.create_task(event_pump())

    try:
        while True:
            raw_msg = await websocket.receive_text()
            try:
                data = json.loads(raw_msg)
            except Exception:
                await websocket.send_json({"type": "error", "message": "Invalid JSON format"})
                continue

            action = data.get("action")

            if action == "ping":
                await websocket.send_json({"type": "pong", "timestamp": data.get("timestamp")})

            elif action == "cancel_batch":
                cancel_event.set()
                await websocket.send_json({"type": "batch_cancelling", "message": "Cancellation requested..."})

            elif action == "run_single":
                payload = data.get("data", {})
                force_refresh = bool(data.get("force_refresh", False))

                crawler_id = str(payload.get("crawler_id", "")).strip() or "crw_single"
                company_name = str(payload.get("company_name", "")).strip() or "Target Company"
                site_id = str(payload.get("site_id", "")).strip() or f"{crawler_id}_SRP"
                career_url = str(payload.get("career_site_url", "")).strip()
                jobs_on_page = int(payload.get("jobs_on_career_page") or payload.get("jobs_on_page") or 0)
                integration_link = str(payload.get("integration_link", "")).strip() or None

                if not career_url:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Career Site Target URL is required.",
                    })
                    continue

                inp = GeneratorInput(
                    crawler_id=crawler_id,
                    company_name=company_name,
                    site_id=site_id,
                    career_site_url=career_url,
                    jobs_on_career_page=jobs_on_page,
                    integration_link=integration_link,
                )

                # Execute pipeline asynchronously in background
                asyncio.create_task(
                    PipelineRunner.run_single(inp, force_refresh, event_queue)
                )

            elif action == "run_batch":
                csv_text = data.get("csv_data", "")
                force_refresh = bool(data.get("force_refresh", False))
                cancel_event.clear()

                if not csv_text.strip():
                    await websocket.send_json({
                        "type": "error",
                        "message": "CSV payload is empty.",
                    })
                    continue

                async def batch_task_wrapper():
                    global _LATEST_RESULTS_CSV, _LATEST_REPLAY_LOGS
                    results, replay_logs = await PipelineRunner.run_batch(
                        csv_text, force_refresh, event_queue, cancel_event
                    )

                    # Save to files and memory
                    if results:
                        output_buf = io.StringIO()
                        writer = csv.DictWriter(output_buf, fieldnames=list(results[0].keys()))
                        writer.writeheader()
                        writer.writerows(results)
                        _LATEST_RESULTS_CSV = output_buf.getvalue()

                        out_csv_path = os.path.join(OUTPUT_DIR, "output_results.csv")
                        with open(out_csv_path, "w", encoding="utf-8", newline="") as f:
                            f.write(_LATEST_RESULTS_CSV)

                    if replay_logs:
                        _LATEST_REPLAY_LOGS = replay_logs
                        out_rep_path = os.path.join(OUTPUT_DIR, "replay_log.json")
                        with open(out_rep_path, "w", encoding="utf-8") as f:
                            json.dump(replay_logs, f, indent=2, ensure_ascii=False)

                asyncio.create_task(batch_task_wrapper())

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    except Exception as exc:
        logger.error("WebSocket server error: %s", exc)
    finally:
        pump_task.cancel()


# Mount static assets from frontend directory
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
