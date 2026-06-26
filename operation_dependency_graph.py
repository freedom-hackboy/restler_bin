#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class OperationDependencyGraphBuilder:
    """
    Build an operation dependency graph from RESTler grammar.py and semantic
    feedback records. The graph is intentionally conservative: it exposes
    scheduling hints for parallel fuzzing without changing RESTler semantics.
    """

    def __init__(
        self,
        grammar_path: str,
        constraints_path: Optional[str] = None,
        applied_constraints_path: Optional[str] = None,
        min_confidence: float = 0.60,
    ):
        self.grammar_path = os.path.abspath(grammar_path)
        self.constraints_path = os.path.abspath(constraints_path) if constraints_path else None
        self.applied_constraints_path = os.path.abspath(applied_constraints_path) if applied_constraints_path else None
        self.min_confidence = min_confidence

    def build(self) -> Dict[str, Any]:
        operations = self._parse_operations()
        operation_index = {operation["operation_id"]: operation for operation in operations}
        constraints = self._load_feedback_records()

        edges: List[Dict[str, Any]] = []
        edges.extend(self._resource_lifecycle_edges(operations))
        edges.extend(self._auth_shared_state_edges(operations))
        edges.extend(self._feedback_edges(operations, constraints))

        deduped_edges = self._dedupe_edges(edges)
        components = self._weakly_connected_components(operation_index, deduped_edges)
        task_packages = self._build_task_packages(components, operation_index, deduped_edges)

        return {
            "schema_version": 1,
            "source": {
                "grammar_path": self.grammar_path,
                "constraints_path": self.constraints_path,
                "applied_constraints_path": self.applied_constraints_path,
                "min_confidence": self.min_confidence,
            },
            "summary": {
                "operation_count": len(operations),
                "edge_count": len(deduped_edges),
                "component_count": len(components),
                "task_package_count": len(task_packages),
                "edge_type_distribution": self._edge_type_distribution(deduped_edges),
            },
            "operations": operations,
            "edges": deduped_edges,
            "weak_components": components,
            "task_packages": task_packages,
        }

    def write(self, graph_path: str, task_plan_path: Optional[str] = None) -> Dict[str, Any]:
        graph = self.build()
        self._write_json(graph_path, graph)
        if task_plan_path:
            task_plan = {
                "schema_version": graph["schema_version"],
                "source_graph": os.path.abspath(graph_path),
                "summary": {
                    "task_package_count": graph["summary"]["task_package_count"],
                    "component_count": graph["summary"]["component_count"],
                },
                "task_packages": graph["task_packages"],
            }
            self._write_json(task_plan_path, task_plan)
        return graph

    def _parse_operations(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.grammar_path):
            raise FileNotFoundError(f"grammar file not found: {self.grammar_path}")

        with open(self.grammar_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        operations: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        block_lines: List[str] = []

        endpoint_pattern = re.compile(
            r"^\s*#\s*Endpoint:\s*(?P<endpoint>.+?),\s*method:\s*(?P<method>[A-Za-z]+)\s*$"
        )
        request_id_pattern = re.compile(r'requestId\s*=\s*"(?P<request_id>[^"]+)"')

        for line_number, line in enumerate(lines, 1):
            endpoint_match = endpoint_pattern.match(line)
            if endpoint_match:
                if current:
                    operations.append(self._finalize_operation(current, block_lines, request_id_pattern))
                endpoint = endpoint_match.group("endpoint").strip()
                method = endpoint_match.group("method").strip().upper()
                current = {
                    "endpoint": endpoint,
                    "method": method,
                    "line_start": line_number,
                }
                block_lines = [line]
                continue

            if current:
                block_lines.append(line)

        if current:
            operations.append(self._finalize_operation(current, block_lines, request_id_pattern))

        operations.sort(key=lambda item: (item["resource_key"], item["endpoint"], item["method"]))
        return operations

    def _finalize_operation(
        self,
        current: Dict[str, Any],
        block_lines: List[str],
        request_id_pattern: re.Pattern,
    ) -> Dict[str, Any]:
        block_text = "".join(block_lines)
        endpoint = current["endpoint"]
        method = current["method"]
        operation_id = f"{method} {endpoint}"
        request_id_match = request_id_pattern.search(block_text)
        path_params = sorted(set(re.findall(r"\{([^{}]+)\}", endpoint)))

        body_params = sorted(set(re.findall(r'"([A-Za-z_][A-Za-z0-9_-]*)"\s*:', block_text)))
        query_params = sorted(set(re.findall(r'restler_static_string\("([A-Za-z_][A-Za-z0-9_-]*)="\)', block_text)))
        header_params = sorted(set(re.findall(r'restler_static_string\("([A-Za-z0-9_-]+):\s*"\)', block_text)))
        auth_required = "restler_refreshable_authentication_token" in block_text

        operation = {
            "operation_id": operation_id,
            "method": method,
            "endpoint": endpoint,
            "request_id": request_id_match.group("request_id") if request_id_match else endpoint,
            "resource_key": self._resource_key(endpoint),
            "collection_key": self._collection_key(endpoint),
            "crud_role": self._crud_role(method, path_params),
            "path_params": path_params,
            "query_params": query_params,
            "body_params": body_params,
            "header_params": header_params,
            "auth_required": auth_required,
            "line_start": current["line_start"],
            "line_end": current["line_start"] + max(0, len(block_lines) - 1),
        }
        return operation

    def _resource_lifecycle_edges(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        edges: List[Dict[str, Any]] = []
        collection_creators: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        consumers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        collection_readers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        modifiers: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for operation in operations:
            method = operation["method"]
            resource_key = operation["resource_key"]
            collection_key = operation["collection_key"]
            path_params = operation["path_params"]

            if method == "POST" and not path_params:
                collection_creators[resource_key].append(operation)
            if path_params:
                consumers[collection_key].append(operation)
            if method == "GET" and not path_params:
                collection_readers[resource_key].append(operation)
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                modifiers[collection_key if path_params else resource_key].append(operation)

        for resource_key, creators in collection_creators.items():
            for creator in creators:
                for consumer in consumers.get(resource_key, []):
                    if creator["operation_id"] == consumer["operation_id"]:
                        continue
                    edges.append(
                        self._edge(
                            creator,
                            consumer,
                            "resource_create_consume",
                            0.88,
                            "Collection POST can create ids consumed by item operations.",
                        )
                    )

        for resource_key, readers in collection_readers.items():
            for reader in readers:
                for consumer in consumers.get(resource_key, []):
                    if reader["operation_id"] == consumer["operation_id"]:
                        continue
                    edges.append(
                        self._edge(
                            reader,
                            consumer,
                            "resource_discovery_consume",
                            0.72,
                            "Collection GET can discover ids consumed by item operations.",
                        )
                    )

        for resource_key, resource_modifiers in modifiers.items():
            deletes = [item for item in resource_modifiers if item["method"] == "DELETE"]
            writes = [item for item in resource_modifiers if item["method"] in {"POST", "PUT", "PATCH"}]
            for write_op in writes:
                for delete_op in deletes:
                    if write_op["operation_id"] == delete_op["operation_id"]:
                        continue
                    edges.append(
                        self._edge(
                            write_op,
                            delete_op,
                            "state_conflict_order",
                            0.68,
                            "Mutating operations should generally run before destructive deletes on the same resource.",
                        )
                    )

        return edges

    def _auth_shared_state_edges(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        edges: List[Dict[str, Any]] = []
        auth_operations = [operation for operation in operations if operation.get("auth_required")]
        for index, source in enumerate(auth_operations):
            for target in auth_operations[index + 1 :]:
                if source["resource_key"] != target["resource_key"]:
                    continue
                edges.append(
                    self._edge(
                        source,
                        target,
                        "shared_auth_context",
                        0.55,
                        "Operations on the same resource share an authentication context.",
                        directed=False,
                    )
                )
        return edges

    def _feedback_edges(
        self,
        operations: List[Dict[str, Any]],
        constraints: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        edges: List[Dict[str, Any]] = []
        operations_by_id = {operation["operation_id"]: operation for operation in operations}
        creators_by_resource = defaultdict(list)
        readers_by_resource = defaultdict(list)

        for operation in operations:
            if operation["method"] == "POST" and not operation["path_params"]:
                creators_by_resource[operation["resource_key"]].append(operation)
            if operation["method"] == "GET" and not operation["path_params"]:
                readers_by_resource[operation["resource_key"]].append(operation)

        for constraint in constraints:
            confidence = self._to_float(
                constraint.get("belief_score", constraint.get("confidence", constraint.get("source_strength", 0.0)))
            )
            if confidence < self.min_confidence:
                continue

            method = str(constraint.get("method", "")).strip().upper()
            endpoint = str(constraint.get("endpoint", "")).strip()
            if not method or not endpoint:
                continue

            target = operations_by_id.get(f"{method} {endpoint}")
            if target is None:
                target = self._find_matching_operation(operations, method, endpoint)
            if target is None:
                continue

            parameter = str(constraint.get("parameter", "")).strip()
            location = str(constraint.get("location", "")).strip().lower()
            constraint_type = str(constraint.get("constraint_type", "")).strip()

            if location == "path" or constraint_type == "not_found":
                for source in creators_by_resource.get(target["collection_key"], []):
                    if source["operation_id"] != target["operation_id"]:
                        edges.append(
                            self._edge(
                                source,
                                target,
                                "feedback_resource_id_dependency",
                                max(0.70, min(0.95, confidence)),
                                f"Feedback suggests parameter '{parameter}' needs an existing resource id.",
                            )
                        )
                for source in readers_by_resource.get(target["collection_key"], []):
                    if source["operation_id"] != target["operation_id"]:
                        edges.append(
                            self._edge(
                                source,
                                target,
                                "feedback_resource_discovery",
                                max(0.62, min(0.90, confidence - 0.05)),
                                f"Feedback suggests parameter '{parameter}' can be satisfied from discovered ids.",
                            )
                        )

            if constraint_type in {"required", "enum", "format", "minimum", "maximum", "pattern", "minLength", "maxLength"}:
                target.setdefault("semantic_parameters", [])
                semantic_param = {
                    "parameter": parameter,
                    "location": location,
                    "constraint_type": constraint_type,
                    "confidence": confidence,
                }
                if semantic_param not in target["semantic_parameters"]:
                    target["semantic_parameters"].append(semantic_param)

        return edges

    def _weakly_connected_components(
        self,
        operation_index: Dict[str, Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        adjacency: Dict[str, Set[str]] = {operation_id: set() for operation_id in operation_index}
        for edge in edges:
            source = edge["source"]
            target = edge["target"]
            if source not in adjacency or target not in adjacency:
                continue
            adjacency[source].add(target)
            adjacency[target].add(source)

        components: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for operation_id in sorted(adjacency):
            if operation_id in seen:
                continue
            queue: deque[str] = deque([operation_id])
            seen.add(operation_id)
            members: List[str] = []
            while queue:
                current = queue.popleft()
                members.append(current)
                for neighbor in sorted(adjacency[current]):
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    queue.append(neighbor)

            component_edges = [
                edge for edge in edges if edge["source"] in members and edge["target"] in members
            ]
            methods = sorted({operation_index[item]["method"] for item in members})
            resources = sorted({operation_index[item]["resource_key"] for item in members})
            components.append(
                {
                    "component_id": f"component_{len(components)}",
                    "operation_ids": sorted(members),
                    "operation_count": len(members),
                    "edge_count": len(component_edges),
                    "methods": methods,
                    "resources": resources,
                    "has_write_operation": any(method in WRITE_METHODS for method in methods),
                }
            )

        components.sort(key=lambda item: (-item["operation_count"], item["component_id"]))
        for index, component in enumerate(components):
            component["component_id"] = f"component_{index}"
        return components

    def _build_task_packages(
        self,
        components: List[Dict[str, Any]],
        operation_index: Dict[str, Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        packages: List[Dict[str, Any]] = []
        for index, component in enumerate(components):
            operation_ids = component["operation_ids"]
            package_edges = [
                edge for edge in edges if edge["source"] in operation_ids and edge["target"] in operation_ids
            ]
            shared_state = sorted(
                {
                    operation_index[operation_id]["resource_key"]
                    for operation_id in operation_ids
                    if operation_index[operation_id]["method"] in WRITE_METHODS
                }
            )
            priority = self._task_priority(component, package_edges)
            packages.append(
                {
                    "task_id": f"task_{index}",
                    "component_id": component["component_id"],
                    "operation_ids": operation_ids,
                    "operation_count": component["operation_count"],
                    "priority": priority,
                    "parallel_safe": not component["has_write_operation"] or len(shared_state) <= 1,
                    "shared_state_keys": shared_state,
                    "edge_type_distribution": self._edge_type_distribution(package_edges),
                    "worker_hints": {
                        "preserve_internal_restler_sequence": True,
                        "share_auth_token": any(operation_index[operation_id].get("auth_required") for operation_id in operation_ids),
                        "share_resource_pool_keys": shared_state,
                    },
                }
            )

        packages.sort(key=lambda item: (-item["priority"], -item["operation_count"], item["task_id"]))
        for index, package in enumerate(packages):
            package["task_id"] = f"task_{index}"
        return packages

    def _task_priority(self, component: Dict[str, Any], edges: List[Dict[str, Any]]) -> float:
        write_bonus = 1.0 if component["has_write_operation"] else 0.0
        feedback_bonus = sum(1 for edge in edges if edge["edge_type"].startswith("feedback_")) * 0.25
        size_bonus = min(2.0, component["operation_count"] / 10.0)
        return round(write_bonus + feedback_bonus + size_bonus, 3)

    def _load_feedback_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for path in [self.constraints_path, self.applied_constraints_path]:
            if not path or not os.path.exists(path):
                continue
            records.extend(self._read_jsonl(path))
        return records

    def _read_jsonl(self, path: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        return records

    def _write_json(self, path: str, data: Dict[str, Any]) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _edge(
        self,
        source: Dict[str, Any],
        target: Dict[str, Any],
        edge_type: str,
        confidence: float,
        evidence: str,
        directed: bool = True,
    ) -> Dict[str, Any]:
        return {
            "source": source["operation_id"],
            "target": target["operation_id"],
            "edge_type": edge_type,
            "confidence": round(float(confidence), 4),
            "directed": directed,
            "evidence": evidence,
        }

    def _dedupe_edges(self, edges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for edge in edges:
            if edge["source"] == edge["target"]:
                continue
            key = (edge["source"], edge["target"], edge["edge_type"])
            existing = deduped.get(key)
            if existing is None or float(edge["confidence"]) > float(existing["confidence"]):
                deduped[key] = edge

        return sorted(
            deduped.values(),
            key=lambda item: (item["source"], item["target"], item["edge_type"]),
        )

    def _edge_type_distribution(self, edges: Iterable[Dict[str, Any]]) -> Dict[str, int]:
        distribution: Dict[str, int] = {}
        for edge in edges:
            edge_type = str(edge.get("edge_type", "unknown"))
            distribution[edge_type] = distribution.get(edge_type, 0) + 1
        return dict(sorted(distribution.items()))

    def _resource_key(self, endpoint: str) -> str:
        parts = self._endpoint_parts(endpoint)
        if not parts:
            return "/"
        if "{" in parts[-1] and len(parts) > 1:
            parts = parts[:-1]
        return "/" + "/".join(parts)

    def _collection_key(self, endpoint: str) -> str:
        parts = self._endpoint_parts(endpoint)
        if parts and "{" in parts[-1]:
            parts = parts[:-1]
        return "/" + "/".join(parts) if parts else "/"

    def _endpoint_parts(self, endpoint: str) -> List[str]:
        return [part for part in endpoint.strip("/").split("/") if part]

    def _crud_role(self, method: str, path_params: List[str]) -> str:
        if method == "GET" and path_params:
            return "read_item"
        if method == "GET":
            return "list"
        if method == "POST" and path_params:
            return "update_item"
        if method == "POST":
            return "create"
        if method in {"PUT", "PATCH"}:
            return "update_item"
        if method == "DELETE":
            return "delete"
        return "other"

    def _find_matching_operation(
        self,
        operations: List[Dict[str, Any]],
        method: str,
        endpoint: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_endpoint = self._normalize_endpoint_template(endpoint)
        for operation in operations:
            if operation["method"] != method:
                continue
            if self._normalize_endpoint_template(operation["endpoint"]) == normalized_endpoint:
                return operation
        return None

    def _normalize_endpoint_template(self, endpoint: str) -> str:
        return re.sub(r"\{[^{}]+\}", "{}", endpoint.strip())

    def _to_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build REST API operation dependency graph and task packages.")
    parser.add_argument("--grammar", required=True, help="RESTler grammar.py path.")
    parser.add_argument("--constraints", default=None, help="semantic_constraints.jsonl path.")
    parser.add_argument("--applied_constraints", default=None, help="applied_constraints.jsonl path.")
    parser.add_argument("--output_graph", required=True, help="Path to write dependency_graph.json.")
    parser.add_argument("--output_tasks", default=None, help="Optional path to write task_plan.json.")
    parser.add_argument("--min_confidence", type=float, default=0.60, help="Minimum feedback confidence used for implicit edges.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    builder = OperationDependencyGraphBuilder(
        grammar_path=args.grammar,
        constraints_path=args.constraints,
        applied_constraints_path=args.applied_constraints,
        min_confidence=args.min_confidence,
    )
    graph = builder.write(args.output_graph, args.output_tasks)
    print(
        "[OperationDependencyGraph] "
        f"operations={graph['summary']['operation_count']} "
        f"edges={graph['summary']['edge_count']} "
        f"components={graph['summary']['component_count']} "
        f"tasks={graph['summary']['task_package_count']}"
    )


if __name__ == "__main__":
    main()
