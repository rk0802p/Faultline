"""OpenAPI specification discovery and workflow mapping.

Implements Spec Section 6.2:
- Imports OpenAPI/Swagger definitions.
- Discovers endpoints, parameters, and operation contracts.
- Constructs workflow dependency graphs.
- Generates recommended failure test scenarios based on endpoint semantics.
"""

import json
from typing import Any


def import_openapi_spec(spec_input: dict[str, Any] | str) -> dict[str, Any]:
    """Parses an OpenAPI 2.0 / 3.0 specification and extracts workflow nodes and failure test recommendations."""
    if isinstance(spec_input, str):
        try:
            spec = json.loads(spec_input)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON specification: {exc}")
    else:
        spec = spec_input

    info = spec.get("info", {})
    title = info.get("title", "Imported API")
    version = info.get("version", "1.0.0")
    description = info.get("description", "")

    paths = spec.get("paths", {})
    discovered_endpoints = []
    workflow_nodes = []
    workflow_edges = []
    recommended_scenarios = []

    node_idx = 0
    prev_node_id = None

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ["get", "post", "put", "delete", "patch"]:
            if method not in path_item:
                continue
            op = path_item[method]
            if not isinstance(op, dict):
                op = {}

            method_upper = method.upper()
            summary = op.get("summary") or op.get("description") or f"{method_upper} {path}"
            operation_id = op.get("operationId") or f"{method}_{path.strip('/').replace('/', '_')}"

            # Extract header parameters to check for idempotency keys
            parameters = op.get("parameters", [])
            headers = [p.get("name") for p in parameters if isinstance(p, dict) and p.get("in") == "header"]
            supports_idempotency = any("idempotency" in str(h).lower() or "request-id" in str(h).lower() for h in headers)

            endpoint_info = {
                "operation_id": operation_id,
                "path": path,
                "method": method_upper,
                "summary": summary,
                "supports_idempotency": supports_idempotency,
                "tags": op.get("tags", []),
            }
            discovered_endpoints.append(endpoint_info)

            # Build workflow node
            node_idx += 1
            node_id = f"node_{node_idx}_{operation_id}"
            node_type = "http_request"
            if "webhook" in path.lower():
                node_type = "webhook_listener"
            elif any(w in path.lower() for w in ["charge", "pay"]):
                node_type = "payment_call"
            elif any(w in path.lower() for w in ["order", "checkout"]):
                node_type = "checkout_action"

            workflow_nodes.append({
                "id": node_id,
                "label": f"{method_upper} {path}",
                "type": node_type,
                "summary": summary,
                "endpoint": path,
                "method": method_upper,
            })

            if prev_node_id:
                workflow_edges.append({
                    "from": prev_node_id,
                    "to": node_id,
                    "type": "sequential",
                })
            prev_node_id = node_id

            # Analyze failure test recommendations
            p_lower = path.lower()
            recommended_scenarios.append({
                "id": f"rec-timeout-{operation_id}",
                "name": f"HTTP Timeout & Latency on {method_upper} {path}",
                "target_endpoint": path,
                "target_method": method_upper,
                "fault_class": "http_timeout",
                "rationale": "External service dependencies can experience unpredictable network delays or dropped connections.",
            })
            if any(term in p_lower for term in ["pay", "charge", "checkout"]):
                recommended_scenarios.append({
                    "id": f"rec-lost-resp-{operation_id}",
                    "name": f"Lost Response After Commit on {method_upper} {path}",
                    "template_id": "payment-response-lost-tpl",
                    "target_endpoint": path,
                    "target_method": method_upper,
                    "fault_class": "payment_response_lost_after_commit",
                    "rationale": "Financial and state-committing endpoints must handle network drops and idempotency gracefully.",
                })
            if "webhook" in p_lower or "callback" in p_lower:
                recommended_scenarios.append({
                    "id": f"rec-dup-webhook-{operation_id}",
                    "name": f"Duplicate Webhook Delivery on {method_upper} {path}",
                    "template_id": "duplicate-payment-webhook-tpl",
                    "target_endpoint": path,
                    "target_method": method_upper,
                    "fault_class": "duplicate_webhook_delivery",
                    "rationale": "Event/webhook listeners must deduplicate redelivered payloads to avoid multiple side-effects.",
                })
            if any(term in p_lower for term in ["order", "record", "persist", "inventory"]):
                recommended_scenarios.append({
                    "id": f"rec-persistence-{operation_id}",
                    "name": f"Partial Persistence Failure on {method_upper} {path}",
                    "template_id": "partial-persistence-failure-tpl",
                    "target_endpoint": path,
                    "target_method": method_upper,
                    "fault_class": "database_persistence_failure",
                    "rationale": "Downstream storage failures after external side effects require compensating actions.",
                })

    return {
        "title": title,
        "version": version,
        "description": description,
        "endpoints_count": len(discovered_endpoints),
        "discovered_endpoints": discovered_endpoints,
        "workflow_nodes": workflow_nodes,
        "workflow_edges": workflow_edges,
        "recommended_scenarios": recommended_scenarios,
    }
