"""Faultline integration packs and pluggable scenario modules."""

from typing import Any


def register_all_integrations(
    fault_registry: Any,
    step_registry: Any,
    assertion_registry: Any,
    schema_registry: Any,
    cleanup_registry: Any,
) -> None:
    """Registers all built-in integration packs into their respective registries."""
    from .payment import register_payment_integration

    register_payment_integration(
        fault_registry=fault_registry,
        step_registry=step_registry,
        assertion_registry=assertion_registry,
        schema_registry=schema_registry,
        cleanup_registry=cleanup_registry,
    )
