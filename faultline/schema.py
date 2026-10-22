"""Shared scenario schema definitions and validation logic (Spec §8 & §14).

Ensures the control plane, store, gap generator, and CLI share an authoritative
definition of supported faults, primary invariants, and parameter schemas.
"""

from typing import Any


SUPPORTED_FAULTS: dict[str, dict[str, Any]] = {
    "payment_response_lost_after_commit": {
        "primary_assertion": "no_duplicate_charge",
        "description": "Provider commits charge, but response is dropped before checkout receives it.",
        "defaults": {"retries": 1},
    },
    "duplicate_payment_webhook": {
        "primary_assertion": "idempotent_webhook_processing",
        "description": "Network redelivery of payment succeeded webhook.",
        "defaults": {"webhook_deliveries": 2, "delay_ms": 100},
    },
    "persistence_failure_after_payment": {
        "primary_assertion": "charge_compensated_on_failure",
        "description": "Database write fails after payment commit; requires compensating refund.",
        "defaults": {"retries": 0, "compensate": True},
    },
}


VALID_WORKFLOW_STEPS: set[str] = {
    "initiate_checkout",
    "process_payment",
    "order_persistence",
    "webhook_delivery",
    "reconciliation_refund",
}


def validate_scenario(definition: dict[str, Any]) -> None:
    """Validates that a scenario definition conforms strictly to supported failure testing constraints."""
    if not isinstance(definition, dict):
        raise ValueError("scenario definition must be a JSON object")

    if type(definition.get("scenario_version")) is not int or definition["scenario_version"] != 1:
        raise ValueError("unsupported scenario_version: must be integer 1")

    fault = definition.get("fault")
    if fault not in SUPPORTED_FAULTS:
        raise ValueError(f"unsupported fault type: '{fault}'. Supported: {list(SUPPORTED_FAULTS.keys())}")

    target_step = definition.get("target_step")
    if target_step is not None and target_step not in VALID_WORKFLOW_STEPS:
        raise ValueError(f"unsupported target_step: '{target_step}'. Valid steps: {sorted(VALID_WORKFLOW_STEPS)}")

    expected_assertion = SUPPORTED_FAULTS[fault]["primary_assertion"]
    if definition.get("assertion") != expected_assertion:
        raise ValueError(
            f"unsupported primary assertion '{definition.get('assertion')}' for fault '{fault}'. "
            f"Expected '{expected_assertion}'."
        )

    if not isinstance(definition.get("name"), str) or not definition["name"].strip():
        raise ValueError("scenario name is required")

    if not isinstance(definition.get("order_id"), str) or not definition["order_id"].strip():
        raise ValueError("order_id is required")

    if type(definition.get("amount_cents")) is not int or definition["amount_cents"] < 1:
        raise ValueError("amount_cents must be a positive integer")

    if fault in {"payment_response_lost_after_commit", "persistence_failure_after_payment"}:
        retries = definition.get("retries")
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ValueError("retries must be an integer between 0 and 3")
    elif fault == "duplicate_payment_webhook":
        deliveries = definition.get("webhook_deliveries", 2)
        if type(deliveries) is not int or not 2 <= deliveries <= 5:
            raise ValueError("webhook_deliveries must be an integer between 2 and 5")
        delay = definition.get("delay_ms", 100)
        if type(delay) is not int or not 0 <= delay <= 5000:
            raise ValueError("delay_ms must be an integer between 0 and 5000")

    timeout = definition.get("timeout_seconds", 15)
    if type(timeout) is not int or not 2 <= timeout <= 60:
        raise ValueError("timeout_seconds must be an integer between 2 and 60")


def validate_template_parameters(schema: dict[str, Any], parameters: dict[str, Any]) -> None:
    """Validates user-provided template parameters against a declared parameter schema."""
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be a dictionary")

    for key, value in parameters.items():
        if key not in schema:
            raise ValueError(f"unknown parameter '{key}'. Allowed parameters: {list(schema.keys())}")

        expected_type = schema[key].get("type")
        if expected_type == "integer":
            if type(value) is not int or isinstance(value, bool):
                raise ValueError(f"parameter '{key}' must be an integer, got {type(value).__name__}")
            if "min" in schema[key] and value < schema[key]["min"]:
                raise ValueError(f"parameter '{key}' must be >= {schema[key]['min']}")
            if "max" in schema[key] and value > schema[key]["max"]:
                raise ValueError(f"parameter '{key}' must be <= {schema[key]['max']}")
        elif expected_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"parameter '{key}' must be a string, got {type(value).__name__}")
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"parameter '{key}' must be a boolean, got {type(value).__name__}")
