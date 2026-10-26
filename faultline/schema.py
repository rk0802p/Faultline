"""Shared scenario schema definitions and validation logic (Spec §8 & §14).

Ensures the control plane, store, gap generator, and CLI share an authoritative,
extensible definition of supported faults, primary invariants, and parameter schemas.
Supports both domain-agnostic generic workflows and pluggable integration packs.
"""

import re
from typing import Any, Callable

TARGET_ID_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

from .security import validate_max_response_bytes


ValidatorCallable = Callable[[dict[str, Any]], None]


class FaultSchemaRegistry:
    """Pluggable registry for fault schemas, primary invariants, and domain-specific validators."""

    def __init__(self) -> None:
        self._faults: dict[str, dict[str, Any]] = {}
        self._validators: dict[str, ValidatorCallable] = {}
        self._legacy_steps: set[str] = set()

    def register(
        self,
        fault_name: str,
        primary_assertion: str,
        description: str,
        category: str = "generic",
        parameter_schema: dict[str, Any] | None = None,
        defaults: dict[str, Any] | None = None,
        validator: ValidatorCallable | None = None,
        legacy_steps: set[str] | None = None,
        pipeline: list[dict[str, Any]] | None = None,
    ) -> None:
        if fault_name in self._faults:
            raise ValueError(f"fault schema '{fault_name}' is already registered")
        self._faults[fault_name] = {
            "primary_assertion": primary_assertion,
            "description": description,
            "category": category,
            "parameter_schema": parameter_schema or {},
            "defaults": defaults or {},
            "pipeline": pipeline or [],
        }
        if validator is not None:
            self._validators[fault_name] = validator
        if legacy_steps:
            self._legacy_steps.update(legacy_steps)

    def get(self, fault_name: str) -> dict[str, Any] | None:
        return self._faults.get(fault_name)

    def is_registered(self, fault_name: str) -> bool:
        return fault_name in self._faults

    def all_faults(self) -> dict[str, dict[str, Any]]:
        return dict(self._faults)

    def get_validator(self, fault_name: str) -> ValidatorCallable | None:
        return self._validators.get(fault_name)

    def legacy_steps(self) -> set[str]:
        return set(self._legacy_steps)


# Global fault schema registry
fault_schema_registry = FaultSchemaRegistry()

# Register core generic fault schemas
fault_schema_registry.register(
    fault_name="http_timeout",
    primary_assertion="all_steps_succeeded_or_handled",
    description="Simulates controlled HTTP timeout against an approved service endpoint.",
    category="network",
    parameter_schema={
        "target_service": {"type": "string", "label": "Target Service", "placeholder": "e.g. checkout-service"},
        "timeout_ms": {"type": "integer", "label": "Timeout Duration (ms)", "min": 100, "max": 30000},
        "timeout_seconds": {"type": "integer", "label": "Run Timeout (seconds)", "min": 2, "max": 60},
    },
    defaults={"timeout_ms": 1500},
    pipeline=[
        {"title": "Client Request", "desc": "Dispatches HTTP request to target service endpoint"},
        {"title": "Inject Latency / Timeout", "desc": "Drops or delays connection exceeding client limit", "is_fault": True},
        {"title": "Retry / Fallback", "desc": "Client circuit breaker or fallback handles timeout"},
        {"title": "Evaluate Invariants", "desc": "Validates boundary protection and graceful degradation"},
    ],
)

# Shared dictionary reference for backward compatibility across modules
SUPPORTED_FAULTS: dict[str, dict[str, Any]] = fault_schema_registry._faults
VALID_WORKFLOW_STEPS: set[str] = fault_schema_registry._legacy_steps


def register_fault_schema(
    fault_name: str,
    primary_assertion: str,
    description: str,
    category: str = "generic",
    parameter_schema: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    validator: ValidatorCallable | None = None,
    legacy_steps: set[str] | None = None,
    pipeline: list[dict[str, Any]] | None = None,
) -> None:
    """Registers a fault schema into the global fault schema registry."""
    fault_schema_registry.register(
        fault_name=fault_name,
        primary_assertion=primary_assertion,
        description=description,
        category=category,
        parameter_schema=parameter_schema,
        defaults=defaults,
        validator=validator,
        legacy_steps=legacy_steps,
        pipeline=pipeline,
    )


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


def register_step_type(step_type: str) -> None:
    """Registers a supported step type for workflow scenario schemas."""
    SUPPORTED_STEP_TYPES.add(step_type)


def validate_scenario(definition: dict[str, Any]) -> None:
    """Validates that a scenario definition conforms to supported constraints via the fault schema registry."""
    if not isinstance(definition, dict):
        raise ValueError("scenario definition must be a JSON object")

    if type(definition.get("scenario_version")) is not int or definition["scenario_version"] != 1:
        raise ValueError("unsupported scenario_version: must be integer 1")




    fault = definition.get("fault")
    if not fault_schema_registry.is_registered(fault):
        raise ValueError(f"unsupported fault type: '{fault}'. Supported: {sorted(list(fault_schema_registry.all_faults().keys()))}")

    target_id = definition.get("target_id")
    if target_id is not None:
        if not isinstance(target_id, str) or not TARGET_ID_REGEX.match(target_id):
            raise ValueError(f"invalid target_id: '{target_id}'")
        from .targets import target_registry
        target = target_registry.get(target_id)
        if target is not None and fault not in target.permitted_faults:
            raise ValueError(
                f"fault '{fault}' is not permitted by target application '{target_id}' (permitted: {sorted(target.permitted_faults)})"
            )

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
        legacy = fault_schema_registry.legacy_steps()
        if target_step is not None and legacy and target_step not in legacy:
            raise ValueError(f"unsupported target_step: '{target_step}'. Valid steps: {sorted(legacy)}")

    fault_info = fault_schema_registry.get(fault)
    expected_assertion = fault_info["primary_assertion"]
    if definition.get("assertion") != expected_assertion:
        raise ValueError(
            f"unsupported primary assertion '{definition.get('assertion')}' for fault '{fault}'. "
            f"Expected '{expected_assertion}'."
        )

    if not isinstance(definition.get("name"), str) or not definition["name"].strip():
        raise ValueError("scenario name is required")

    # Domain-specific validation: dispatched via registered validator callback if present
    custom_validator = fault_schema_registry.get_validator(fault)
    if custom_validator is not None:
        custom_validator(definition)
    else:
        # Generic non-integration validations
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


def __getattr__(name: str) -> Any:
    if name == "PAYMENT_FAULTS":
        import warnings
        from .integrations.payment import PAYMENT_FAULTS
        warnings.warn(
            "PAYMENT_FAULTS in faultline.schema is deprecated; import from faultline.integrations.payment instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return PAYMENT_FAULTS
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
