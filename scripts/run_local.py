#!/usr/bin/env python3
"""
Run the Content Machine locally (all services).

Usage:
    python scripts/run_local.py           # Run web + bot
    python scripts/run_local.py --web     # Web only
    python scripts/run_local.py --bot     # Bot only
"""
import os
import sys
import subprocess
import argparse
import signal
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
os.chdir(project_root)

# Load .env
from dotenv import load_dotenv
load_dotenv()


def run_web():
    """Run the web dashboard"""
    print("Starting web dashboard at http://localhost:8000")
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "web.app:app",
        "--host", "127.0.0.1",
        "--port", "8000",
        "--reload"
    ])


def run_bot():
    """Run the Telegram bot"""
    print("Starting Telegram bot...")
    subprocess.run([sys.executable, "-m", "src.bot"])


def run_both():
    """Run both web and bot in parallel"""
    import multiprocessing

    web_process = multiprocessing.Process(target=run_web)
    bot_process = multiprocessing.Process(target=run_bot)

    def cleanup(signum, frame):
        print("\nShutting down...")
        web_process.terminate()
        bot_process.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    web_process.start()
    bot_process.start()

    print("\nContent Machine running!")
    print("  Web: http://localhost:8000")
    print("  Bot: Check Telegram")
    print("\nPress Ctrl+C to stop\n")

    web_process.join()
    bot_process.join()


def main():
    parser = argparse.ArgumentParser(description="Run Content Machine locally")
    parser.add_argument("--web", action="store_true", help="Run web only")
    parser.add_argument("--bot", action="store_true", help="Run bot only")
    args = parser.parse_args()

    if args.web:
        run_web()
    elif args.bot:
        run_bot()
    else:
        run_both()


if __name__ == "__main__":
    main()
