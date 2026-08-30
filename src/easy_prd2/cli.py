from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the Easy PRD2 local web app")
    result.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    result.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    return result


def main() -> None:
    args = parser().parse_args()
    if not 1 <= args.port <= 65535:
        parser().error("--port must be between 1 and 65535")
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Easy PRD2 is running at {url}")
    uvicorn.run("easy_prd2.app:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

