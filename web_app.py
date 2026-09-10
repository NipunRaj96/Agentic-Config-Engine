"""
web_app.py
──────────
CLI launcher for the AI-Config-Engine TechOps Dashboard and WebSocket Server.

Usage:
  python web_app.py [--host 0.0.0.0] [--port 8000] [--reload]
"""

import argparse
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load environment variables from .env if present
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                _val = _v.strip()
                if (_val.startswith('"') and _val.endswith('"')) or (_val.startswith("'") and _val.endswith("'")):
                    _val = _val[1:-1]
                os.environ[_k.strip()] = _val


def main():
    parser = argparse.ArgumentParser(
        description="AI-Config-Engine TechOps Web Console",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for dev")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("Error: 'uvicorn' is required to run the web server. Install it with: pip install uvicorn fastapi")
        sys.exit(1)

    print(f"\n========================================================")
    print(f"  AI-Config-Engine | TechOps Telemetry Console")
    print(f"  Server starting on http://localhost:{args.port}")
    print(f"  WebSocket endpoint at ws://localhost:{args.port}/ws/pipeline")
    print(f"========================================================\n")

    uvicorn.run("src.web.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
