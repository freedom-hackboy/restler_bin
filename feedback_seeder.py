#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import copy
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set


WORDPRESS_PARAMETER_DEFAULTS = {
    "title": ["restler-title", "semantic-title"],
    "content": ["restler-content", "semantic-content"],
    "excerpt": ["restler-excerpt"],
    "name": ["restler-name", "semantic-name"],
    "username": ["restler_user"],
    "email": ["restler_user@example.com"],
    "password": ["StrongPass123!"],
    "status": ["draft", "publish", "private"],
    "orderby": ["id", "date", "title", "slug", "name"],
    "reassign": ["1"],
    "post_id": ["1"],
    "parent": ["0", "1"],
}

RESOURCE_PARAM_HINTS = {
    "/wp-json/wp/v2/pages": ["id", "pageId", "parent"],
    "/wp-json/wp/v2/posts": ["id", "postId", "post_id", "post", "parent"],
    "/wp-json/wp/v2/categories": ["id", "categoryId", "categories", "parent"],
    "/wp-json/wp/v2/tags": ["id", "tagId", "tags"],
    "/wp-json/wp/v2/comments": ["id", "commentId"],
    "/wp-json/wp/v2/comment_hosts": ["post", "post_id"],
    "/wp-json/wp/v2/users": ["id", "userId", "author", "reassign"],
    "/wp-json/wp/v2/settings": ["id", "settingId"],
}

ENDPOINT_SPECIFIC_QUERY_PARAMS = {
    "_embed",
    "_fields",
    "context",
    "offset",
    "order",
    "orderby",
    "page",
    "per_page",
    "search",
    "slug",
    "status",
}

PROTECTED_WORDPRESS_RESOURCE_IDS = {
    "/wp-json/wp/v2/categories": {"1"},
    "/wp-json/wp/v2/comments": {"1"},
    "/wp-json/wp/v2/users": {"1"},
}


class FeedbackSeeder:
    """
    Convert semantic feedback and parallel resource pools into RESTler
    dictionary candidates. This is the bridge between iterative semantic
    learning and dependency-graph parallel testing.
    """

    DEFAULT_SCHEMA = {
        "restler_fuzzable_string": [],
        "restler_fuzzable_string_unquoted": [],
        "restler_fuzzable_int": [],
        "restler_fuzzable_number": [],
        "restler_fuzzable_bool": [],
        "restler_custom_payload": {},
        "restler_custom_payload_unquoted": {},
        "restler_custom_payload_query": {},
        "restler_custom_payload_query_unquoted": {},
        "restler_custom_payload_header": {},
        "restler_custom_payload_header_unquoted": {},
    }

    def __init__(
        self,
        min_confidence: float = 0.60,
        include_wordpress_defaults: bool = True,
        prune_endpoint_query_payloads: bool = True,
        include_endpoint_query_semantics: bool = False,
    ):
        self.min_confidence = min_confidence
        self.include_wordpress_defaults = include_wordpress_defaults
        self.prune_endpoint_query_payloads = prune_endpoint_query_payloads
        self.include_endpoint_query_semantics = include_endpoint_query_semantics

    def seed(
        self,
        base_dict_path: str,
        output_dict_path: str,
        semantic_constraints_paths: Optional[List[str]] = None,
        resource_pool_paths: Optional[List[str]] = None,
        task_plan_path: Optional[str] = None,
        operation_ids: Optional[List[str]] = None,
        resource_keys: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        dictionary = self._prepare_dictionary(self._load_json(base_dict_path))
        operation_ids = [str(item) for item in (operation_ids or [])]
        resource_keys = [str(item) for item in (resource_keys or [])]
        semantic_constraints = [
            item
            for item in self._load_constraints(semantic_constraints_paths or [])
            if self._constraint_matches_operations(item, operation_ids)
        ]
        resource_pool = self._load_resource_pool(resource_pool_paths or [], resource_keys=resource_keys)
        task_resource_params = self._extract_resource_params_from_task_plan(
            task_plan_path,
            operation_ids=operation_ids,
            resource_keys=resource_keys,
        )

        summary = {
            "base_dict_path": os.path.abspath(base_dict_path),
            "output_dict_path": os.path.abspath(output_dict_path),
            "semantic_constraint_count": len(semantic_constraints),
            "resource_pool_count": len(resource_pool),
            "operation_scope": operation_ids,
            "resource_scope": resource_keys,
            "seeded_parameters": {},
            "seeded_resource_values": {},
            "wordpress_defaults_enabled": self.include_wordpress_defaults,
            "prune_endpoint_query_payloads": self.prune_endpoint_query_payloads,
            "include_endpoint_query_semantics": self.include_endpoint_query_semantics,
        }

        for constraint in semantic_constraints:
            self._apply_semantic_constraint(dictionary, constraint, summary)

        self._apply_resource_pool(dictionary, resource_pool, task_resource_params, summary)
        self._dedupe_dictionary(dictionary)
        self._save_json(output_dict_path, dictionary)
        return summary

    def _apply_semantic_constraint(
        self,
        dictionary: Dict[str, Any],
        constraint: Dict[str, Any],
        summary: Dict[str, Any],
    ) -> None:
        confidence = self._to_float(
            constraint.get("belief_score", constraint.get("confidence", constraint.get("source_strength", 0.0)))
        )
        if confidence < self.min_confidence:
            return
        if self._looks_like_route_level_failure(constraint):
            return

        parameter = str(constraint.get("parameter", "")).strip()
        if not parameter or parameter == "unknown":
            return
        location = str(constraint.get("location", "")).strip().lower() or "body"
        if (
            location == "query"
            and parameter in ENDPOINT_SPECIFIC_QUERY_PARAMS
            and not self.include_endpoint_query_semantics
        ):
            return
        ctype = str(constraint.get("constraint_type", "")).strip()
        values: List[Any] = []

        if ctype == "enum" and isinstance(constraint.get("constraint_value"), list):
            values.extend(constraint["constraint_value"])
        elif ctype == "required":
            values.extend(self._default_values_for_parameter(parameter, constraint))
        elif ctype in {"format", "pattern"}:
            values.extend(self._default_values_for_parameter(parameter, constraint))
        elif ctype == "not_found" and location == "path":
            # Path ids should come from producer endpoints or the shared resource
            # pool. Seeding a fixed id from 404 feedback tends to break dynamic
            # object dependencies in worker-local grammars.
            return
        elif ctype == "not_found" and location == "query":
            values.extend(self._default_values_for_parameter(parameter, constraint))

        if not values:
            return

        self._add_parameter_values(dictionary, parameter, location, values, add_to_fuzzable_pool=False)
        self._record_seed(summary, "seeded_parameters", parameter, values)

    def _apply_resource_pool(
        self,
        dictionary: Dict[str, Any],
        resource_pool: Dict[str, List[Any]],
        task_resource_params: Dict[str, Set[str]],
        summary: Dict[str, Any],
    ) -> None:
        all_ids: List[Any] = []
        for resource_key, ids in resource_pool.items():
            if not ids:
                continue
            ids = self._filter_resource_ids(resource_key, ids)
            if not ids:
                continue
            all_ids.extend(ids)
            params = set(RESOURCE_PARAM_HINTS.get(resource_key, [])) | set(task_resource_params.get(resource_key, set()))
            for parameter in sorted(params):
                self._add_parameter_values(dictionary, parameter, "path", ids, add_to_fuzzable_pool=True)
                self._record_seed(summary, "seeded_resource_values", parameter, ids)

        numeric_ids = [str(int(value)) for value in all_ids if self._looks_like_int(value)]
        string_ids = [str(value) for value in all_ids]
        self._extend_list(dictionary["restler_fuzzable_int"], numeric_ids)
        self._extend_list(dictionary["restler_fuzzable_string"], string_ids)
        self._extend_list(dictionary["restler_fuzzable_string_unquoted"], string_ids)

    def _default_values_for_parameter(self, parameter: str, constraint: Dict[str, Any]) -> List[str]:
        normalized = parameter.strip()
        if normalized == "name":
            return [f"restler-name-{int(time.time())}"]
        if self.include_wordpress_defaults and normalized in WORDPRESS_PARAMETER_DEFAULTS:
            return list(WORDPRESS_PARAMETER_DEFAULTS[normalized])

        lowered = normalized.lower()
        if lowered.endswith("id") or lowered == "id":
            return ["1"]
        if "email" in lowered:
            return ["restler_user@example.com"]
        if "password" in lowered:
            return ["StrongPass123!"]
        if "name" in lowered:
            return [f"{normalized}-{int(time.time())}"]
        if "title" in lowered:
            return ["restler-title"]
        if "content" in lowered:
            return ["restler-content"]
        if "status" in lowered:
            return ["draft", "publish"]
        return [f"{normalized}-value"]

    def _add_parameter_values(
        self,
        dictionary: Dict[str, Any],
        parameter: str,
        location: str,
        values: Iterable[Any],
        add_to_fuzzable_pool: bool = True,
    ) -> None:
        string_values = [str(value) for value in values if value is not None and str(value) != ""]
        if not string_values:
            return

        if location == "query":
            target_keys = ["restler_custom_payload_query", "restler_custom_payload_query_unquoted"]
        elif location == "header":
            target_keys = ["restler_custom_payload_header", "restler_custom_payload_header_unquoted"]
        else:
            target_keys = ["restler_custom_payload", "restler_custom_payload_unquoted"]

        for key in target_keys:
            dictionary.setdefault(key, {})
            dictionary[key].setdefault(parameter, [])
            self._extend_list(dictionary[key][parameter], string_values)

        if not add_to_fuzzable_pool:
            return

        if any(self._looks_like_int(value) for value in string_values):
            self._extend_list(dictionary["restler_fuzzable_int"], [str(int(value)) for value in string_values if self._looks_like_int(value)])
        self._extend_list(dictionary["restler_fuzzable_string"], string_values)
        self._extend_list(dictionary["restler_fuzzable_string_unquoted"], string_values)

    def _load_constraints(self, paths: List[str]) -> List[Dict[str, Any]]:
        constraints: List[Dict[str, Any]] = []
        for path in paths:
            if not path or not os.path.exists(path):
                continue
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
                        constraints.append(item)
        return constraints

    def _load_resource_pool(self, paths: List[str], resource_keys: Optional[List[str]] = None) -> Dict[str, List[Any]]:
        resources: Dict[str, List[Any]] = {}
        allowed_keys = {str(item) for item in resource_keys or []}
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            data = self._load_json(path)
            raw_resources = data.get("resources", {}) if isinstance(data, dict) else {}
            if not isinstance(raw_resources, dict):
                continue
            for resource_key, entry in raw_resources.items():
                if allowed_keys and str(resource_key) not in allowed_keys:
                    continue
                if not isinstance(entry, dict) or not isinstance(entry.get("ids"), list):
                    continue
                resources.setdefault(str(resource_key), [])
                self._extend_list(resources[str(resource_key)], self._filter_resource_ids(str(resource_key), entry["ids"]))
        return resources

    def _filter_resource_ids(self, resource_key: str, ids: Iterable[Any]) -> List[Any]:
        protected = PROTECTED_WORDPRESS_RESOURCE_IDS.get(str(resource_key), set())
        return [item for item in ids if str(item) not in protected]

    def _looks_like_route_level_failure(self, item: Dict[str, Any]) -> bool:
        status_code = str(item.get("last_status_code", item.get("status", ""))).strip()
        if status_code != "404":
            return False
        evidence = self._normalize_text(
            item.get("last_evidence_text")
            or item.get("error_signature")
            or item.get("evidence_text")
            or item.get("evidence")
            or ""
        )
        markers = [
            "未找到匹配 url 和请求方式的路由",
            "未找到匹配 url 和请求方法的路由",
            "no route was found matching the url and request method",
            "no route was found matching the url",
            "rest_no_route",
        ]
        return any(marker in evidence for marker in markers)

    def _normalize_text(self, value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    def _extract_resource_params_from_task_plan(
        self,
        task_plan_path: Optional[str],
        operation_ids: Optional[List[str]] = None,
        resource_keys: Optional[List[str]] = None,
    ) -> Dict[str, Set[str]]:
        mapping: Dict[str, Set[str]] = {}
        if not task_plan_path or not os.path.exists(task_plan_path):
            return mapping
        allowed_operations = set(operation_ids or [])
        allowed_resources = set(resource_keys or [])
        data = self._load_json(task_plan_path)
        for task in data.get("task_packages", []):
            task_resources = [str(item) for item in task.get("shared_state_keys", [])]
            if allowed_resources:
                task_resources = [item for item in task_resources if item in allowed_resources]
            if not task_resources:
                continue
            for operation_id in task.get("operation_ids", []):
                if allowed_operations and str(operation_id) not in allowed_operations:
                    continue
                for name in re.findall(r"\{([^{}]+)\}", str(operation_id)):
                    for resource_key in task_resources:
                        mapping.setdefault(resource_key, set()).add(name)
        return mapping

    def _constraint_matches_operations(self, constraint: Dict[str, Any], operation_ids: List[str]) -> bool:
        if not operation_ids:
            return True
        method = str(constraint.get("method", "")).strip().upper()
        endpoint = str(constraint.get("endpoint", "")).strip()
        if not method or not endpoint:
            return False
        constraint_id = f"{method} {endpoint}"
        if constraint_id in operation_ids:
            return True
        normalized_constraint = self._normalize_operation_id(constraint_id)
        return normalized_constraint in {self._normalize_operation_id(item) for item in operation_ids}

    def _normalize_operation_id(self, operation_id: str) -> str:
        method, _, endpoint = str(operation_id).partition(" ")
        normalized_endpoint = re.sub(r"\{[^{}]+\}", "{}", endpoint.strip())
        return f"{method.strip().upper()} {normalized_endpoint}"

    def _prepare_dictionary(self, dictionary: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(dictionary)
        for key, default in self.DEFAULT_SCHEMA.items():
            if key not in result or not isinstance(result[key], type(default)):
                result[key] = copy.deepcopy(default)
        if self.prune_endpoint_query_payloads:
            for key in ["restler_custom_payload_query", "restler_custom_payload_query_unquoted"]:
                payloads = result.get(key, {})
                if not isinstance(payloads, dict):
                    continue
                for parameter in ENDPOINT_SPECIFIC_QUERY_PARAMS:
                    payloads.pop(parameter, None)
        return result

    def _dedupe_dictionary(self, dictionary: Dict[str, Any]) -> None:
        for key, value in list(dictionary.items()):
            if isinstance(value, list):
                dictionary[key] = self._dedupe_values(value)
            elif isinstance(value, dict):
                for child_key, child_value in list(value.items()):
                    if isinstance(child_value, list):
                        value[child_key] = self._dedupe_values(child_value)

    def _record_seed(self, summary: Dict[str, Any], bucket: str, parameter: str, values: Iterable[Any]) -> None:
        target = summary.setdefault(bucket, {})
        target.setdefault(parameter, [])
        self._extend_list(target[parameter], [str(value) for value in values])

    def _dedupe_values(self, values: Iterable[Any]) -> List[str]:
        result: List[str] = []
        self._extend_list(result, [str(value) for value in values if value is not None and str(value) != ""])
        return result

    def _extend_list(self, target: List[Any], values: Iterable[Any]) -> None:
        existing = {str(item) for item in target}
        for value in values:
            normalized = str(value)
            if normalized in existing:
                continue
            target.append(normalized)
            existing.add(normalized)

    def _looks_like_int(self, value: Any) -> bool:
        return re.fullmatch(r"-?\d+", str(value)) is not None

    def _to_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _load_json(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, path: str, data: Dict[str, Any]) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed RESTler dictionary from semantic feedback and resource pools.")
    parser.add_argument("--base_dict", required=True)
    parser.add_argument("--output_dict", required=True)
    parser.add_argument("--constraints", nargs="*", default=[])
    parser.add_argument("--resource_pool", nargs="*", default=[])
    parser.add_argument("--task_plan", default=None)
    parser.add_argument("--min_confidence", type=float, default=0.60)
    parser.add_argument("--disable_wordpress_defaults", action="store_true")
    parser.add_argument("--keep_endpoint_query_payloads", action="store_true")
    parser.add_argument("--include_endpoint_query_semantics", action="store_true")
    parser.add_argument("--operation_id", action="append", default=[])
    parser.add_argument("--resource_key", action="append", default=[])
    parser.add_argument("--summary", default=None)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    seeder = FeedbackSeeder(
        min_confidence=args.min_confidence,
        include_wordpress_defaults=not args.disable_wordpress_defaults,
        prune_endpoint_query_payloads=not args.keep_endpoint_query_payloads,
        include_endpoint_query_semantics=args.include_endpoint_query_semantics,
    )
    summary = seeder.seed(
        base_dict_path=args.base_dict,
        output_dict_path=args.output_dict,
        semantic_constraints_paths=args.constraints,
        resource_pool_paths=args.resource_pool,
        task_plan_path=args.task_plan,
        operation_ids=args.operation_id,
        resource_keys=args.resource_key,
    )
    if args.summary:
        seeder._save_json(args.summary, summary)
    print(
        "[FeedbackSeeder] "
        f"parameters={len(summary.get('seeded_parameters', {}))} "
        f"resource_parameters={len(summary.get('seeded_resource_values', {}))} "
        f"output={args.output_dict}"
    )


if __name__ == "__main__":
    main()
