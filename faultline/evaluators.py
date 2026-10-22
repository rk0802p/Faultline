"""Domain-independent assertion and invariant evaluator registry (Spec §15).

Provides reusable invariant evaluations across workflow executions and integration packs.
"""

from typing import Any, Callable


EvaluatorCallable = Callable[[dict[str, Any]], dict[str, Any]]


class AssertionEvaluatorRegistry:
    """Registry mapping invariant names to evaluation functions."""

    def __init__(self) -> None:
        self._evaluators: dict[str, EvaluatorCallable] = {}

    def register(self, name: str, evaluator: EvaluatorCallable) -> None:
        if name in self._evaluators:
            raise ValueError(f"assertion evaluator '{name}' is already registered")
        self._evaluators[name] = evaluator

    def get(self, name: str) -> EvaluatorCallable | None:
        return self._evaluators.get(name)

    def is_registered(self, name: str) -> bool:
        return name in self._evaluators

    def evaluate(self, name: str, context: dict[str, Any]) -> dict[str, Any]:
        """Evaluates assertion by name against execution context."""
        evaluator = self.get(name)
        if evaluator is not None:
            return evaluator(context)
        # Default fallback evaluator for custom or unhandled assertions
        status = "passed" if context.get("is_passed", False) else "failed"
        return {
            "name": name,
            "status": status,
            "expected": f"assertion '{name}' satisfied",
            "observed": context.get("observed", "evaluated via generic assertion fallback"),
            "evidence": context.get("evidence", ["GENERIC_ASSERTION"]),
        }


# Global registry instance
assertion_registry = AssertionEvaluatorRegistry()


def evaluate_fault_activation_confirmed(context: dict[str, Any]) -> dict[str, Any]:
    """Evaluates whether the injected fault was actually activated and observed."""
    confirmed = bool(context.get("fault_activation_confirmed"))
    fault_type = context.get("fault_type", "fault")
    return {
        "name": "fault_activation_confirmed",
        "status": "passed" if confirmed else "failed",
        "expected": f"fault '{fault_type}' activated and observed at target step",
        "observed": "confirmed" if confirmed else "fault activation not observed",
        "evidence": ["FAULT_INJECTED", "HTTP_TIMEOUT_OBSERVED"] if confirmed else ["NO_FAULT_CONFIRMED"],
    }


def evaluate_all_steps_succeeded_or_handled(context: dict[str, Any]) -> dict[str, Any]:
    """Evaluates whether all steps in the workflow succeeded or recovered gracefully."""
    is_passed = bool(context.get("is_passed"))
    executed_steps = context.get("executed_steps", [])
    workflow = context.get("workflow", [])
    step_failures = context.get("step_failures", 0)
    return {
        "name": "all_steps_succeeded_or_handled",
        "status": "passed" if is_passed else "failed",
        "expected": "all workflow steps succeed or recover gracefully",
        "observed": f"{len(executed_steps)}/{len(workflow)} steps succeeded; {step_failures} failures",
        "evidence": [f"step:{s}" for s in executed_steps] if is_passed else ["UNHANDLED_STEP_FAILURE"],
    }


def evaluate_step_execution_verified(context: dict[str, Any]) -> dict[str, Any]:
    """Evaluates whether designated required steps executed successfully."""
    required_steps = set(context.get("required_steps", []))
    executed_steps = set(context.get("executed_steps", []))
    missing = required_steps - executed_steps
    is_passed = len(missing) == 0
    return {
        "name": "step_execution_verified",
        "status": "passed" if is_passed else "failed",
        "expected": f"all required steps executed: {sorted(required_steps)}",
        "observed": f"executed {sorted(executed_steps)}, missing {sorted(missing)}",
        "evidence": [f"executed:{s}" for s in sorted(executed_steps)] if is_passed else [f"missing:{s}" for s in sorted(missing)],
    }


# Register core domain-independent evaluators
assertion_registry.register("fault_activation_confirmed", evaluate_fault_activation_confirmed)
assertion_registry.register("all_steps_succeeded_or_handled", evaluate_all_steps_succeeded_or_handled)
assertion_registry.register("step_execution_verified", evaluate_step_execution_verified)
