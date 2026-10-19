"""Command-line client for the Faultline control-plane API."""

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def api_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Faultline scenario through the local control-plane API")
    parser.add_argument("run", choices=["run"])
    parser.add_argument("--scenario", required=True, help="scenario ID")
    parser.add_argument("--implementation", choices=["vulnerable", "fixed"], default="vulnerable")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    try:
        submitted = api_json(base + "/api/runs", {"scenario_id": args.scenario, "implementation": args.implementation})
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            result = api_json(base + "/api/runs/" + submitted["run_id"])["run"]
            if result["status"] in {"completed", "failed"}:
                print(json.dumps(result, indent=2))
                if result["status"] == "failed": return 2
                return 0 if result["verdict"] == "passed" else 1
            time.sleep(0.25)
        print(f"Timed out waiting for run {submitted['run_id']}", file=sys.stderr)
        return 3
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as exc:
        print(f"Faultline API error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
