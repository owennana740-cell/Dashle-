"""Render Cron entry point: securely ask the web service to run due tasks."""

import os
import sys

import requests


def main():
    url = os.environ.get("DASHLE_CRON_URL", "").strip()
    secret = os.environ.get("CRON_SECRET", "")
    if not url.startswith("https://") or not secret:
        print("Cron configuration is incomplete (URL/secret required).", file=sys.stderr)
        return 2
    try:
        response = requests.post(url, headers={"X-Cron-Secret": secret}, timeout=100)
        response.raise_for_status()
        print("Scheduled task request completed.")
        return 0
    except requests.RequestException as exc:
        print(f"Scheduled task request failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
