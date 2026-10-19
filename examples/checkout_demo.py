"""Dependency-free Faultline kickoff demo: payment response lost after commit.

Run with: python examples/checkout_demo.py

This is a deterministic domain simulation, not a network fault proxy. It makes
the key ambiguity (side effect committed, response lost) explicit and testable.
"""

from dataclasses import dataclass
from typing import Optional


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

        if self._lose_first_response:
            self._lose_first_response = False
            raise ResponseLost("provider committed charge; response was lost")
        return charge


def checkout(provider: PaymentProvider, order_id: str, *, fixed: bool) -> int:
    """Retry once after timeout; return number of payment attempts."""
    idempotency_key = f"checkout:{order_id}" if fixed else None
    attempts = 0
    for _ in range(2):
        attempts += 1
        try:
            provider.charge(order_id, 2500, idempotency_key)
            break
        except ResponseLost:
            # The first response is known to be lost, but the side effect may
            # already have happened. Retry once to model the vulnerable client.
            continue
    return attempts


def run_case(label: str, *, fixed: bool) -> bool:
    order_id = "order-001"
    provider = PaymentProvider()
    attempts = checkout(provider, order_id, fixed=fixed)
    order_charges = [c for c in provider.charges if c.order_id == order_id]
    passed = len(order_charges) <= 1
    print(f"{label}")
    print(f"  order: {order_id}")
    print(f"  payment attempts: {attempts}")
    print(f"  successful charges: {len(order_charges)}")
    print(f"  invariant no_duplicate_charge: {'PASS' if passed else 'FAIL'}")
    return passed


def main() -> int:
    vulnerable_passed = run_case("Vulnerable checkout", fixed=False)
    fixed_passed = run_case("Idempotent checkout", fixed=True)
    # Expected demo outcome: the seeded defect is caught and the fix passes.
    return 0 if not vulnerable_passed and fixed_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
