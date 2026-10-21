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
    parser.add_argument("--debug", action="store_true", help="enable detailed debugging and request diagnostics")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    try:
        if args.debug:
            print(f"[debug] Submitting run: scenario={args.scenario} implementation={args.implementation} to {base}/api/runs", file=sys.stderr)
        submitted = api_json(base + "/api/runs", {"scenario_id": args.scenario, "implementation": args.implementation})
        run_id = submitted["run_id"]
        if args.debug:
            print(f"[debug] Run submitted successfully: run_id={run_id}", file=sys.stderr)
        deadline = time.monotonic() + args.timeout
        poll_count = 0
        while time.monotonic() < deadline:
            poll_count += 1
            result = api_json(base + "/api/runs/" + run_id)["run"]
            if args.debug:
                print(f"[debug] Poll #{poll_count}: status={result['status']} verdict={result.get('verdict')}", file=sys.stderr)
            if result["status"] in {"completed", "failed", "cancelled", "timed_out"}:
                print(json.dumps(result, indent=2))
                if result["status"] == "failed": return 2
                if result["status"] == "timed_out": return 3
                if result["status"] == "cancelled": return 4
                return 0 if result["verdict"] == "passed" else 1
            time.sleep(0.25)
        print(f"Timed out waiting for run {run_id}", file=sys.stderr)
        return 3
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as exc:
        print(f"Faultline API error: {exc}", file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
