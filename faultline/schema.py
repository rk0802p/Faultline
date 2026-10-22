"""Shared scenario schema definitions and validation logic (Spec §8 & §14).

Ensures the control plane, store, gap generator, and CLI share an authoritative
definition of supported faults, primary invariants, and parameter schemas.
Supports both reference payment integrations and extensible, generic workflows.
"""

from typing import Any

from .security import validate_max_response_bytes


PAYMENT_FAULTS: set[str] = {
    "payment_response_lost_after_commit",
    "duplicate_payment_webhook",
    "persistence_failure_after_payment",
}


SUPPORTED_FAULTS: dict[str, dict[str, Any]] = {
    "payment_response_lost_after_commit": {
        "primary_assertion": "no_duplicate_charge",
        "description": "Provider commits charge, but response is dropped before checkout receives it.",
        "defaults": {"retries": 1},
        "category": "payment",
        "parameter_schema": {
            "order_id": {"type": "string", "label": "Order ID", "placeholder": "order-001"},
            "amount_cents": {"type": "integer", "label": "Charge Amount (cents)", "min": 1},
            "retries": {"type": "integer", "label": "Retry Attempts", "min": 0, "max": 3},
            "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
        },
    },
    "duplicate_payment_webhook": {
        "primary_assertion": "idempotent_webhook_processing",
        "description": "Network redelivery of payment succeeded webhook.",
        "defaults": {"webhook_deliveries": 2, "delay_ms": 100},
        "category": "webhook",
        "parameter_schema": {
            "order_id": {"type": "string", "label": "Order ID", "placeholder": "order-001"},
            "amount_cents": {"type": "integer", "label": "Charge Amount (cents)", "min": 1},
            "webhook_deliveries": {"type": "integer", "label": "Webhook Deliveries", "min": 2, "max": 5},
            "delay_ms": {"type": "integer", "label": "Delivery Delay (ms)", "min": 0, "max": 5000},
            "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
        },
    },
    "persistence_failure_after_payment": {
        "primary_assertion": "charge_compensated_on_failure",
        "description": "Database write fails after payment commit; requires compensating refund.",
        "defaults": {"retries": 0, "compensate": True},
        "category": "storage",
        "parameter_schema": {
            "order_id": {"type": "string", "label": "Order ID", "placeholder": "order-001"},
            "amount_cents": {"type": "integer", "label": "Charge Amount (cents)", "min": 1},
            "retries": {"type": "integer", "label": "Retry Attempts", "min": 0, "max": 3},
            "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
        },
    },
    "http_timeout": {
        "primary_assertion": "all_steps_succeeded_or_handled",
        "description": "Simulates controlled HTTP timeout against an approved service endpoint.",
        "defaults": {"timeout_ms": 1500},
        "category": "network",
        "parameter_schema": {
            "target_service": {"type": "string", "label": "Target Service", "placeholder": "e.g. checkout-service"},
            "timeout_ms": {"type": "integer", "label": "Timeout Duration (ms)", "min": 100, "max": 30000},
            "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
        },
    },
}


VALID_WORKFLOW_STEPS: set[str] = {
    "initiate_checkout",
    "process_payment",
    "order_persistence",
    "webhook_delivery",
    "reconciliation_refund",
}

SUPPORTED_STEP_TYPES: set[str] = {
    "http.request",
    "http.get",
    "http.post",
    "db.operation",
    "db.write",
    "db.read",
    "generic.operation",
}

HTTP_STEP_TYPES: set[str] = {
    "http.request",
    "http.get",
    "http.post",
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

    workflow = definition.get("workflow")
    target_step = definition.get("target_step")

    if workflow is not None:
        if not isinstance(workflow, list) or not workflow:
            raise ValueError("workflow must be a non-empty list of step definitions")
        seen_step_ids: set[str] = set()
        step_by_id: dict[str, dict[str, Any]] = {}
        for idx, step in enumerate(workflow):
            if not isinstance(step, dict):
                raise ValueError(f"workflow step #{idx+1} must be an object")
            step_id = step.get("id")
            if not step_id or not isinstance(step_id, str):
                raise ValueError(f"workflow step #{idx+1} missing valid 'id'")
            if step_id in seen_step_ids:
                raise ValueError(f"duplicate workflow step id '{step_id}'")
            seen_step_ids.add(step_id)
            step_by_id[step_id] = step
            step_type = step.get("type")
            if not isinstance(step_type, str) or not step_type.strip():
                raise ValueError(f"workflow step '{step_id}' requires a valid 'type'")
            if step_type not in SUPPORTED_STEP_TYPES:
                raise ValueError(
                    f"unsupported workflow step type: '{step_type}'. Supported step types: {sorted(SUPPORTED_STEP_TYPES)}"
                )

            # Validate max_response_bytes if specified at step or step config level
            raw_mrb = step.get("max_response_bytes") if "max_response_bytes" in step else (
                step.get("config", {}).get("max_response_bytes") if isinstance(step.get("config"), dict) else None
            )
            if raw_mrb is not None:
                try:
                    validate_max_response_bytes(raw_mrb)
                except ValueError as exc:
                    raise ValueError(f"workflow step '{step_id}' invalid max_response_bytes: {exc}")

        if target_step is not None:
            if target_step not in seen_step_ids:
                raise ValueError(f"unsupported target_step: '{target_step}'. Valid steps: {sorted(seen_step_ids)}")
            target_obj = step_by_id[target_step]
            target_type = target_obj.get("type")
            if fault == "http_timeout" and target_type not in HTTP_STEP_TYPES:
                raise ValueError(
                    f"target_step '{target_step}' with type '{target_type}' is incompatible with fault 'http_timeout'; "
                    f"must be an HTTP operation ({sorted(HTTP_STEP_TYPES)})"
                )
    else:
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

    # Payment-specific schema validations: only required when fault is a payment fault
    if fault in PAYMENT_FAULTS:
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
    else:
        # Generic / non-payment fault validations
        if "timeout_ms" in definition:
            t_ms = definition["timeout_ms"]
            if type(t_ms) is not int or not 100 <= t_ms <= 30000:
                raise ValueError("timeout_ms must be an integer between 100 and 30000")

    timeout = definition.get("timeout_seconds", 15)
    if type(timeout) is not int or not 2 <= timeout <= 60:
        raise ValueError("timeout_seconds must be an integer between 2 and 60")

    if "max_response_bytes" in definition and definition["max_response_bytes"] is not None:
        try:
            validate_max_response_bytes(definition["max_response_bytes"])
        except ValueError as exc:
            raise ValueError(f"scenario invalid max_response_bytes: {exc}")

    raw_fault_cfg = definition.get("fault_config")
    if raw_fault_cfg is not None:
        if not isinstance(raw_fault_cfg, dict):
            raise ValueError("fault_config must be an object")
        if "client_timeout_seconds" in raw_fault_cfg:
            try:
                ct = float(raw_fault_cfg["client_timeout_seconds"])
                if ct <= 0:
                    raise ValueError("client_timeout_seconds must be positive (> 0)")
            except (ValueError, TypeError):
                raise ValueError("fault_config client_timeout_seconds must be a positive number")
        if "server_delay_seconds" in raw_fault_cfg:
            try:
                sd = float(raw_fault_cfg["server_delay_seconds"])
                if sd < 0:
                    raise ValueError("server_delay_seconds must be non-negative (>= 0)")
            except (ValueError, TypeError):
                raise ValueError("fault_config server_delay_seconds must be a non-negative number")


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
