"""Run the local payment-response-lost scenario against both implementations.

Run with: python examples/checkout_demo.py

This is a deterministic domain simulation, not a network fault proxy.
"""

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


class ResponseLost(TimeoutError):
    """The provider completed an operation but checkout did not receive a reply."""


@dataclass(frozen=True)
class Charge:
    order_id: str
    amount_cents: int
    idempotency_key: Optional[str]


class PaymentProvider:
    """Test provider with an inspectable side-effect ledger."""

    def __init__(self) -> None:
        self.charges: list[Charge] = []
        self._by_key: dict[str, Charge] = {}
        self._lose_first_response = True
        self.events: list[dict[str, Any]] = []

    def charge(
        self, order_id: str, amount_cents: int, idempotency_key: Optional[str]
    ) -> Charge:
        if idempotency_key is not None and idempotency_key in self._by_key:
            charge = self._by_key[idempotency_key]
        else:
            charge = Charge(order_id, amount_cents, idempotency_key)
            self.charges.append(charge)
            if idempotency_key is not None:
                self._by_key[idempotency_key] = charge
            self.events.append({"type": "charge_committed", "order_id": order_id})

        if self._lose_first_response:
            self._lose_first_response = False
            self.events.append({"type": "payment_response_lost", "order_id": order_id})
            raise ResponseLost("provider committed charge; response was lost")
        return charge


def load_scenario(path: Path) -> dict[str, Any]:
    scenario = json.loads(path.read_text(encoding="utf-8"))
    if scenario.get("scenario_version") != 1:
        raise ValueError("scenario_version must be 1")
    if scenario.get("fault") != "payment_response_lost_after_commit":
        raise ValueError("unsupported fault; expected payment_response_lost_after_commit")
    if scenario.get("assertion") != "no_duplicate_charge":
        raise ValueError("unsupported assertion; expected no_duplicate_charge")
    if not isinstance(scenario.get("order_id"), str) or not scenario["order_id"]:
        raise ValueError("order_id must be a non-empty string")
    if not isinstance(scenario.get("amount_cents"), int) or scenario["amount_cents"] <= 0:
        raise ValueError("amount_cents must be a positive integer")
    if scenario.get("retries") != 1:
        raise ValueError("this demo currently supports exactly one retry")
    return scenario


def checkout(
    provider: PaymentProvider,
    order_id: str,
    amount_cents: int,
    retries: int,
    *,
    fixed: bool,
) -> int:
    """Retry after timeout; return number of payment attempts."""
    idempotency_key = f"checkout:{order_id}" if fixed else None
    attempts = 0
    for attempt in range(retries + 1):
        attempts += 1
        provider.events.append({"type": "payment_attempt_started", "attempt": attempt + 1})
        try:
            provider.charge(order_id, amount_cents, idempotency_key)
            break
        except ResponseLost:
            # The first response is known to be lost, but the side effect may
            # already have happened. Retry once to model the vulnerable client.
            provider.events.append({"type": "payment_retry_scheduled", "attempt": attempt + 1})
            continue
    return attempts


def run_case(scenario: dict[str, Any], implementation: str) -> dict[str, Any]:
    fixed = implementation == "fixed"
    order_id = scenario["order_id"]
    provider = PaymentProvider()
    attempts = checkout(
        provider,
        order_id,
        scenario["amount_cents"],
        scenario["retries"],
        fixed=fixed,
    )
    order_charges = [c for c in provider.charges if c.order_id == order_id]
    passed = len(order_charges) <= 1
    return {
        "scenario": scenario["name"],
        "implementation": implementation,
        "order_id": order_id,
        "attempts": attempts,
        "charges": [asdict(charge) for charge in order_charges],
        "assertions": [{"name": scenario["assertion"], "status": "passed" if passed else "failed"}],
        "events": provider.events,
        "status": "passed" if passed else "failed",
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"{report['implementation'].title()} checkout — {report['scenario']}")
    print(f"  order: {report['order_id']}")
    print(f"  payment attempts: {report['attempts']}")
    print(f"  successful charges: {len(report['charges'])}")
    assertion = report["assertions"][0]
    print(f"  invariant {assertion['name']}: {assertion['status'].upper()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Faultline checkout scenario")
    parser.add_argument(
        "--scenario",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "scenarios" / "payment-response-lost.json",
    )
    parser.add_argument("--implementation", choices=("vulnerable", "fixed", "both"), default="both")
    parser.add_argument("--json", action="store_true", help="print the full run evidence as JSON")
    args = parser.parse_args()
    try:
        scenario = load_scenario(args.scenario)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    implementations = ("vulnerable", "fixed") if args.implementation == "both" else (args.implementation,)
    reports = [run_case(scenario, implementation) for implementation in implementations]
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        for report in reports:
            print_report(report)

    # In both mode, the vulnerable run is expected to fail and the fixed run to pass.
    expected = {"vulnerable": "failed", "fixed": "passed"}
    return 0 if all(report["status"] == expected[report["implementation"]] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
