"""Faultline integration packs and pluggable scenario modules."""

import importlib
import logging
from typing import Any, Callable

logger = logging.getLogger("faultline.integrations")

DEFAULT_INTEGRATION_MODULES: list[str] = [
    "faultline.integrations.payment",
]


class IntegrationManager:
    """Manages discoverable integration packs and plugins."""

    def __init__(self) -> None:
        self._loaded_modules: set[str] = set()

    def load_integration(
        self,
        module_or_name: str | Any,
        fault_registry: Any,
        step_registry: Any,
        assertion_registry: Any,
        schema_registry: Any,
        cleanup_registry: Any,
    ) -> str:
        """Loads and registers an integration pack. Rejects duplicate registrations."""
        if isinstance(module_or_name, str):
            mod_name = module_or_name
            if mod_name in self._loaded_modules:
                raise ValueError(f"integration module '{mod_name}' is already loaded")
            mod = importlib.import_module(mod_name)
        else:
            mod = module_or_name
            mod_name = getattr(mod, "__name__", str(mod))
            if mod_name in self._loaded_modules:
                raise ValueError(f"integration module '{mod_name}' is already loaded")

        # Discover registration function
        register_fn: Callable[..., None] | None = None
        for candidate in ("register_integration", "register_all_integrations"):
            if hasattr(mod, candidate):
                register_fn = getattr(mod, candidate)
                break

        if register_fn is None:
            for attr in dir(mod):
                if attr.startswith("register_") and attr.endswith("_integration"):
                    register_fn = getattr(mod, attr)
                    break

        if register_fn is None:
            raise ValueError(
                f"module '{mod_name}' does not define a recognizable registration function "
                f"(expected 'register_integration' or 'register_*_integration')"
            )

        register_fn(
            fault_registry=fault_registry,
            step_registry=step_registry,
            assertion_registry=assertion_registry,
            schema_registry=schema_registry,
            cleanup_registry=cleanup_registry,
        )
        self._loaded_modules.add(mod_name)
        logger.debug("successfully loaded integration pack '%s'", mod_name)
        return mod_name

    def load_all(
        self,
        modules: list[str] | None = None,
        fault_registry: Any = None,
        step_registry: Any = None,
        assertion_registry: Any = None,
        schema_registry: Any = None,
        cleanup_registry: Any = None,
    ) -> list[str]:
        """Loads a list of integration modules. Defaults to DEFAULT_INTEGRATION_MODULES."""
        target_modules = DEFAULT_INTEGRATION_MODULES if modules is None else modules
        loaded = []
        for mod in target_modules:
            loaded.append(
                self.load_integration(
                    mod,
                    fault_registry=fault_registry,
                    step_registry=step_registry,
                    assertion_registry=assertion_registry,
                    schema_registry=schema_registry,
                    cleanup_registry=cleanup_registry,
                )
            )
        return loaded

    def is_loaded(self, module_name: str) -> bool:
        return module_name in self._loaded_modules

    def loaded_integrations(self) -> set[str]:
        return set(self._loaded_modules)

    def reset(self) -> None:
        """Clears the set of loaded integration module names."""
        self._loaded_modules.clear()


integration_manager = IntegrationManager()


def register_all_integrations(
    fault_registry: Any,
    step_registry: Any,
    assertion_registry: Any,
    schema_registry: Any,
    cleanup_registry: Any,
    modules: list[str] | None = None,
) -> list[str]:
    """Loads and registers integration packs into their respective registries."""
    return integration_manager.load_all(
        modules=modules,
        fault_registry=fault_registry,
        step_registry=step_registry,
        assertion_registry=assertion_registry,
        schema_registry=schema_registry,
        cleanup_registry=cleanup_registry,
    )
