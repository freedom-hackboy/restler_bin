#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from feedback_seeder import FeedbackSeeder, PROTECTED_WORDPRESS_RESOURCE_IDS, RESOURCE_PARAM_HINTS


SUPPORTED_MODES = {"test", "fuzz", "fuzz-lean"}


class TaskGrammarWriter:
    def __init__(self, source_grammar_path: str):
        self.source_grammar_path = os.path.abspath(source_grammar_path)
        self.preamble, self.blocks = self._parse_grammar()

    def write_task_grammar(self, operation_ids: List[str], output_path: str) -> Dict[str, Any]:
        selected_ids = set(operation_ids)
        selected_blocks = [block for block in self.blocks if block["operation_id"] in selected_ids]
        missing_ids = sorted(selected_ids - {block["operation_id"] for block in selected_blocks})

        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(self.preamble)
            if self.preamble and not self.preamble.endswith("\n"):
                f.write("\n")
            for block in selected_blocks:
                f.write(block["text"])
                if not block["text"].endswith("\n"):
                    f.write("\n")

        return {
            "source_grammar": self.source_grammar_path,
            "output_grammar": os.path.abspath(output_path),
            "requested_operation_count": len(operation_ids),
            "written_operation_count": len(selected_blocks),
            "missing_operation_ids": missing_ids,
        }

    def _parse_grammar(self) -> Tuple[str, List[Dict[str, Any]]]:
        if not os.path.exists(self.source_grammar_path):
            raise FileNotFoundError(f"grammar file not found: {self.source_grammar_path}")

        with open(self.source_grammar_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        endpoint_pattern = re.compile(
            r"^\s*#\s*Endpoint:\s*(?P<endpoint>.+?),\s*method:\s*(?P<method>[A-Za-z]+)\s*$"
        )
        blocks: List[Dict[str, Any]] = []
        preamble_lines: List[str] = []
        block_lines: List[str] = []
        current: Optional[Dict[str, Any]] = None

        for line in lines:
            endpoint_match = endpoint_pattern.match(line)
            if endpoint_match:
                if current:
                    current["text"] = "".join(block_lines)
                    blocks.append(current)
                endpoint = endpoint_match.group("endpoint").strip()
                method = endpoint_match.group("method").strip().upper()
                current = {
                    "operation_id": f"{method} {endpoint}",
                    "method": method,
                    "endpoint": endpoint,
                    "path_params": sorted(set(re.findall(r"\{([^{}]+)\}", endpoint))),
                }
                block_lines = [line]
                continue

            if current:
                block_lines.append(line)
            else:
                preamble_lines.append(line)

        if current:
            current["text"] = "".join(block_lines)
            blocks.append(current)

        return "".join(preamble_lines), blocks


class TaskSemanticGrammarPatcher:
    def __init__(self, constraints_paths: List[str], min_confidence: float = 0.60):
        self.constraints_paths = constraints_paths
        self.min_confidence = min_confidence
        self.constraints = self._load_constraints(constraints_paths)

    def patch(self, grammar_path: str, operation_ids: List[str]) -> Dict[str, Any]:
        operation_set = set(str(item) for item in operation_ids)
        plans = self._build_plans(operation_set)
        if not plans:
            return {
                "enabled": bool(self.constraints_paths),
                "patched": False,
                "body_fields_added": [],
                "query_fields_added": [],
                "query_primitives_replaced": [],
            }

        with open(grammar_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        patched_lines: List[str] = []
        block: List[str] = []
        current_operation: Optional[str] = None
        summary = {
            "enabled": True,
            "patched": False,
            "body_fields_added": [],
            "query_fields_added": [],
            "query_primitives_replaced": [],
        }

        endpoint_pattern = re.compile(
            r"^\s*#\s*Endpoint:\s*(?P<endpoint>.+?),\s*method:\s*(?P<method>[A-Za-z]+)\s*$"
        )

        def flush_block() -> None:
            nonlocal block, current_operation
            if not block:
                return
            if current_operation and current_operation in plans:
                patched_lines.extend(self._patch_block(block, current_operation, plans[current_operation], summary))
            else:
                patched_lines.extend(block)
            block = []

        for line in lines:
            match = endpoint_pattern.match(line)
            if match:
                flush_block()
                current_operation = f"{match.group('method').strip().upper()} {match.group('endpoint').strip()}"
                block = [line]
                continue
            if current_operation:
                block.append(line)
            else:
                patched_lines.append(line)
        flush_block()

        if summary["patched"]:
            with open(grammar_path, "w", encoding="utf-8") as f:
                f.writelines(patched_lines)

        return summary

    def _build_plans(self, operation_set: Set[str]) -> Dict[str, Dict[str, List[Dict[str, str]]]]:
        plans: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
        normalized_operations = {self._normalize_operation_id(item): item for item in operation_set}

        for constraint in self.constraints:
            confidence = self._to_float(
                constraint.get("belief_score", constraint.get("confidence", constraint.get("source_strength", 0.0)))
            )
            if confidence < self.min_confidence:
                continue
            if self._looks_like_route_level_failure(constraint):
                continue
            method = str(constraint.get("method", "")).strip().upper()
            endpoint = str(constraint.get("endpoint", "")).strip()
            if not method or not endpoint:
                continue
            operation_id = f"{method} {endpoint}"
            matched_operation = operation_id if operation_id in operation_set else normalized_operations.get(self._normalize_operation_id(operation_id))
            if not matched_operation:
                continue

            parameter = str(constraint.get("parameter", "")).strip()
            location = str(constraint.get("location", "")).strip().lower()
            ctype = str(constraint.get("constraint_type", "")).strip()
            if not parameter or parameter == "unknown":
                continue

            plan = plans.setdefault(matched_operation, {"body": [], "query_add": [], "query_replace": []})
            if location == "body" and ctype == "required":
                field_name, payload_name = self._body_field_alias(endpoint, parameter)
                plan["body"].append({"field": field_name, "payload": payload_name})
            elif location == "query" and ctype in {"required", "enum", "not_found", "format", "pattern"}:
                plan["query_replace"].append({"field": parameter, "payload": parameter})
                if ctype == "required":
                    plan["query_add"].append({"field": parameter, "payload": parameter})

        for plan in plans.values():
            for key in ["body", "query_add", "query_replace"]:
                plan[key] = self._dedupe_field_specs(plan[key])
        return plans

    def _patch_block(
        self,
        block: List[str],
        operation_id: str,
        plan: Dict[str, List[Dict[str, str]]],
        summary: Dict[str, Any],
    ) -> List[str]:
        patched = list(block)
        for spec in plan.get("query_replace", []):
            patched, changed = self._replace_query_primitive(patched, spec["field"], spec["payload"])
            if changed:
                summary["patched"] = True
                summary["query_primitives_replaced"].append({"operation_id": operation_id, **spec})

        for spec in plan.get("query_add", []):
            if self._block_contains_query_parameter(patched, spec["field"]):
                continue
            patched, changed = self._add_query_parameter(patched, spec["field"], spec["payload"])
            if changed:
                summary["patched"] = True
                summary["query_fields_added"].append({"operation_id": operation_id, **spec})

        for spec in plan.get("body", []):
            if self._block_contains_body_field(patched, spec["field"]):
                continue
            patched, changed = self._add_body_field(patched, spec["field"], spec["payload"])
            if changed:
                summary["patched"] = True
                summary["body_fields_added"].append({"operation_id": operation_id, **spec})
        return patched

    def _replace_query_primitive(self, lines: List[str], field: str, payload: str) -> Tuple[List[str], bool]:
        result = list(lines)
        for index, line in enumerate(result[:-1]):
            if f'primitives.restler_static_string("{field}=")' not in line:
                continue
            next_index = index + 1
            if "primitives.restler_custom_payload_query(" in result[next_index]:
                return result, False
            if "primitives.restler_fuzzable_" not in result[next_index]:
                return result, False
            indent = result[next_index][: len(result[next_index]) - len(result[next_index].lstrip())]
            comma = "," if result[next_index].rstrip().endswith(",") else ""
            result[next_index] = f'{indent}primitives.restler_custom_payload_query("{payload}", quoted=False){comma}\n'
            return result, True
        return result, False

    def _add_query_parameter(self, lines: List[str], field: str, payload: str) -> Tuple[List[str], bool]:
        for index, line in enumerate(lines):
            if 'primitives.restler_static_string(" HTTP/1.1\\r\\n")' not in line:
                continue
            has_query = any('primitives.restler_static_string("?")' in item for item in lines[:index])
            sep = "&" if has_query else "?"
            indent = line[: len(line) - len(line.lstrip())]
            insert = [
                f'{indent}primitives.restler_static_string("{sep}"),\n',
                f'{indent}primitives.restler_static_string("{field}="),\n',
                f'{indent}primitives.restler_custom_payload_query("{payload}", quoted=False),\n',
            ]
            return lines[:index] + insert + lines[index:], True
        return lines, False

    def _add_body_field(self, lines: List[str], field: str, payload: str) -> Tuple[List[str], bool]:
        for index, line in enumerate(lines):
            if 'primitives.restler_static_string("}"),' not in line:
                continue
            indent = line[: len(line) - len(line.lstrip())]
            insert = [
                f'{indent}primitives.restler_static_string("""\n    "{field}":"""),\n',
                f'{indent}primitives.restler_custom_payload("{payload}", quoted=True),\n',
            ]
            if self._body_has_any_field(lines[:index]):
                insert[0] = f'{indent}primitives.restler_static_string(""",\n    "{field}":"""),\n'
            return lines[:index] + insert + lines[index:], True
        return lines, False

    def _block_contains_query_parameter(self, lines: List[str], field: str) -> bool:
        return any(f'primitives.restler_static_string("{field}=")' in line for line in lines)

    def _block_contains_body_field(self, lines: List[str], field: str) -> bool:
        return re.search(rf'"{re.escape(field)}"\s*:', "".join(lines)) is not None

    def _body_has_any_field(self, lines: List[str]) -> bool:
        return re.search(r'"[^"\r\n]+"\s*:', "".join(lines)) is not None

    def _body_field_alias(self, endpoint: str, parameter: str) -> Tuple[str, str]:
        if endpoint.startswith("/wp-json/wp/v2/comments") and parameter == "post_id":
            return "post", "post"
        return parameter, parameter

    def _dedupe_field_specs(self, specs: List[Dict[str, str]]) -> List[Dict[str, str]]:
        seen = set()
        result = []
        for spec in specs:
            key = (spec["field"], spec["payload"])
            if key in seen:
                continue
            seen.add(key)
            result.append(spec)
        return result

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

    def _normalize_operation_id(self, operation_id: str) -> str:
        method, _, endpoint = str(operation_id).partition(" ")
        normalized_endpoint = re.sub(r"\{[^{}]+\}", "{}", endpoint.strip())
        return f"{method.strip().upper()} {normalized_endpoint}"

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

    def _to_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


class ParallelTaskScheduler:
    def __init__(
        self,
        project_root: str = ".",
        task_plan_path: str = "task_plan.json",
        grammar_path: str = "grammar.py",
        dict_path: str = "dict.json",
        settings_path: str = "engine_settings.json",
        output_dir: str = "parallel_runs",
        restler_mode: str = "test",
        max_workers: int = 2,
        no_ssl: bool = True,
        time_budget: Optional[float] = None,
        search_strategy: Optional[str] = None,
        host: Optional[str] = None,
        target_ip: Optional[str] = None,
        target_port: Optional[int] = None,
        task_timeout_sec: Optional[int] = None,
        continue_on_worker_failure: bool = True,
        limit_tasks: Optional[int] = None,
        prepare_only: bool = False,
        resource_pool_path: Optional[str] = None,
        seed_workers_from_resource_pool: bool = True,
        semantic_constraints_paths: Optional[List[str]] = None,
        seed_workers_from_semantics: bool = True,
        semantic_seed_min_confidence: float = 0.60,
        enable_resource_path_patch: bool = False,
        defer_delete_operations: bool = False,
    ):
        self.project_root = os.path.abspath(project_root)
        self.task_plan_path = self._resolve_path(task_plan_path)
        self.grammar_path = self._resolve_path(grammar_path)
        self.dict_path = self._resolve_path(dict_path)
        self.settings_path = self._resolve_path(settings_path)
        self.output_dir = self._resolve_path(output_dir)
        self.restler_mode = restler_mode.lower().strip()
        self.max_workers = max(1, int(max_workers))
        self.no_ssl = no_ssl
        self.time_budget = time_budget
        self.search_strategy = search_strategy
        self.host = host
        self.target_ip = target_ip
        self.target_port = target_port
        self.task_timeout_sec = task_timeout_sec
        self.continue_on_worker_failure = continue_on_worker_failure
        self.limit_tasks = limit_tasks
        self.prepare_only = prepare_only
        self.resource_pool_path = self._resolve_path(resource_pool_path) if resource_pool_path else None
        self.seed_workers_from_resource_pool = seed_workers_from_resource_pool
        self.semantic_constraints_paths = [
            self._resolve_path(path) for path in (semantic_constraints_paths or []) if path
        ]
        self.seed_workers_from_semantics = seed_workers_from_semantics
        self.semantic_seed_min_confidence = semantic_seed_min_confidence
        self.enable_resource_path_patch = enable_resource_path_patch
        self.defer_delete_operations = defer_delete_operations

        if self.restler_mode not in SUPPORTED_MODES:
            supported = ", ".join(sorted(SUPPORTED_MODES))
            raise ValueError(f"Unsupported restler_mode: {self.restler_mode}. Supported: {supported}")
        if not os.path.exists(self.task_plan_path):
            raise FileNotFoundError(f"task plan not found: {self.task_plan_path}")
        if not os.path.exists(self.grammar_path):
            raise FileNotFoundError(f"grammar file not found: {self.grammar_path}")
        if not os.path.exists(self.dict_path):
            raise FileNotFoundError(f"dictionary file not found: {self.dict_path}")
        if not os.path.exists(self.settings_path):
            raise FileNotFoundError(f"settings file not found: {self.settings_path}")

        self.restler_exe = os.path.join(self.project_root, "restler", "Restler.exe")
        if not os.path.exists(self.restler_exe):
            raise FileNotFoundError(f"RESTler executable not found: {self.restler_exe}")

    def run(self) -> Dict[str, Any]:
        started_at = time.time()
        task_plan = self._load_json(self.task_plan_path)
        task_packages = list(task_plan.get("task_packages", []))
        if self.defer_delete_operations:
            task_packages = self._split_delete_operations(task_packages)
        task_packages.sort(key=lambda item: (-float(item.get("priority", 0.0) or 0.0), item.get("task_id", "")))
        if self.limit_tasks is not None:
            task_packages = task_packages[: max(0, int(self.limit_tasks))]

        run_id = time.strftime("parallel-%Y%m%d-%H%M%S")
        run_dir = os.path.join(self.output_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)

        grammar_writer = TaskGrammarWriter(self.grammar_path)
        shared_resource_pool = self._load_resource_pool(self.resource_pool_path)
        prepared_tasks = [
            self._prepare_task(run_dir, task_package, grammar_writer, shared_resource_pool)
            for task_package in task_packages
        ] if self.prepare_only else []

        schedule = {
            "schema_version": 1,
            "run_id": run_id,
            "run_dir": run_dir,
            "started_at": self._timestamp(started_at),
            "project_root": self.project_root,
            "restler_mode": self.restler_mode,
            "max_workers": self.max_workers,
            "task_count": len(task_packages if not self.prepare_only else prepared_tasks),
            "resource_pool_path": self.resource_pool_path,
            "seed_workers_from_resource_pool": self.seed_workers_from_resource_pool,
            "semantic_constraints_paths": self.semantic_constraints_paths,
            "seed_workers_from_semantics": self.seed_workers_from_semantics,
            "semantic_seed_min_confidence": self.semantic_seed_min_confidence,
            "enable_resource_path_patch": self.enable_resource_path_patch,
            "defer_delete_operations": self.defer_delete_operations,
            "tasks": [
                {
                    "task_id": str(task_package.get("task_id", "")),
                    "operation_count": len(task_package.get("operation_ids", [])),
                    "priority": task_package.get("priority"),
                    "shared_state_keys": task_package.get("shared_state_keys", []),
                }
                for task_package in task_packages
            ],
        }
        self._write_json(os.path.join(run_dir, "schedule.json"), schedule)

        if self.prepare_only:
            summary = self._summarize(run_dir, started_at, [])
            summary["task_count"] = len(prepared_tasks)
            summary["prepared_task_count"] = len(prepared_tasks)
            summary["completed_task_count"] = 0
            summary["failed_task_count"] = 0
            summary["prepare_only"] = True
            summary["results"] = []
            summary["resource_pool"] = {
                "schema_version": 1,
                "resource_count": 0,
                "resources": {},
                "note": "Prepare-only run; workers were not executed.",
            }
            self._write_json(os.path.join(run_dir, "summary.json"), summary)
            return summary

        results: List[Dict[str, Any]] = []
        pending_packages = list(task_packages)
        batch_index = 0
        while pending_packages:
            batch_packages = pending_packages[: self.max_workers]
            pending_packages = pending_packages[self.max_workers :]
            batch_dir = os.path.join(run_dir, f"batch_{batch_index}")
            os.makedirs(batch_dir, exist_ok=True)
            batch_tasks = [
                self._prepare_task(batch_dir, task_package, grammar_writer, shared_resource_pool)
                for task_package in batch_packages
            ]
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_task = {
                    executor.submit(self._run_worker, task): task
                    for task in batch_tasks
                }
                for future in concurrent.futures.as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        if not self.continue_on_worker_failure:
                            raise
                        result = self._failed_task_result(task, str(exc))
                    results.append(result)

            batch_pool = self._build_resource_pool(results)
            shared_resource_pool = self._merge_resource_pools(shared_resource_pool, batch_pool)
            shared_resource_pool["last_updated_at"] = self._timestamp(time.time())
            shared_resource_pool["completed_batches"] = batch_index + 1
            self._write_resource_pool(shared_resource_pool, os.path.join(run_dir, "resource_pool.live.json"))
            if self.resource_pool_path:
                self._write_resource_pool(shared_resource_pool, self.resource_pool_path)
            self._write_json(os.path.join(run_dir, "partial_summary.json"), self._summarize(run_dir, started_at, results))
            batch_index += 1

        summary = self._summarize(run_dir, started_at, results)
        summary["resource_pool"] = shared_resource_pool
        self._write_json(os.path.join(run_dir, "resource_pool.json"), summary["resource_pool"])
        self._write_json(os.path.join(run_dir, "summary.json"), summary)
        return summary

    def _prepare_task(
        self,
        run_dir: str,
        task_package: Dict[str, Any],
        grammar_writer: TaskGrammarWriter,
        resource_pool: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        task_id = str(task_package.get("task_id", f"task_{int(time.time())}"))
        safe_task_id = self._safe_name(task_id)
        worker_dir = os.path.join(run_dir, safe_task_id)
        os.makedirs(worker_dir, exist_ok=True)

        task_grammar_path = os.path.join(worker_dir, "grammar.py")
        grammar_summary = grammar_writer.write_task_grammar(
            operation_ids=list(task_package.get("operation_ids", [])),
            output_path=task_grammar_path,
        )
        task_dict_path = os.path.join(worker_dir, "dict.json")
        shutil.copy2(self.dict_path, task_dict_path)
        dictionary_seed_summary = {
            "semantic_feedback": self._seed_task_semantics(task_dict_path, task_package),
            "resource_pool": self._seed_task_dictionary(task_dict_path, task_package, grammar_writer, resource_pool),
        }
        grammar_patch_summary = self._patch_task_grammar(task_grammar_path, task_package)
        resource_path_patch_summary = (
            self._patch_resource_path_primitives(
                task_grammar_path,
                task_package,
                dictionary_seed_summary["resource_pool"].get("parameter_names", []),
            )
            if self.enable_resource_path_patch
            and int(dictionary_seed_summary["resource_pool"].get("seeded_value_count", 0) or 0) > 0
            else {"patched": False, "path_primitives_replaced": []}
        )
        grammar_patch_summary["path_primitives_replaced"] = resource_path_patch_summary.get("path_primitives_replaced", [])
        grammar_patch_summary["patched"] = bool(
            grammar_patch_summary.get("patched") or resource_path_patch_summary.get("patched")
        )

        metadata = {
            "task_package": task_package,
            "grammar_summary": grammar_summary,
            "grammar_patch_summary": grammar_patch_summary,
            "dictionary_seed_summary": dictionary_seed_summary,
            "dict_path": task_dict_path,
            "settings_path": self.settings_path,
        }
        self._write_json(os.path.join(worker_dir, "task_metadata.json"), metadata)

        return {
            "task_id": task_id,
            "worker_dir": worker_dir,
            "task_grammar_path": task_grammar_path,
            "task_dict_path": task_dict_path,
            "task_package": task_package,
            "grammar_summary": grammar_summary,
            "grammar_patch_summary": grammar_patch_summary,
            "dictionary_seed_summary": dictionary_seed_summary,
        }

    def _split_delete_operations(self, task_packages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        split_packages: List[Dict[str, Any]] = []
        for task_package in task_packages:
            operations = [str(item) for item in task_package.get("operation_ids", [])]
            delete_operations = [item for item in operations if item.upper().startswith("DELETE ")]
            non_delete_operations = [item for item in operations if not item.upper().startswith("DELETE ")]

            if non_delete_operations:
                clone = dict(task_package)
                clone["task_id"] = f"{task_package.get('task_id', 'task')}_non_delete"
                clone["operation_ids"] = non_delete_operations
                clone["operation_count"] = len(non_delete_operations)
                clone["priority"] = float(task_package.get("priority", 0.0) or 0.0) + 0.25
                clone["split_reason"] = "defer_delete_operations"
                split_packages.append(clone)

            if delete_operations:
                clone = dict(task_package)
                clone["task_id"] = f"{task_package.get('task_id', 'task')}_delete"
                clone["operation_ids"] = delete_operations
                clone["operation_count"] = len(delete_operations)
                clone["priority"] = float(task_package.get("priority", 0.0) or 0.0) - 100.0
                clone["split_reason"] = "defer_delete_operations"
                split_packages.append(clone)

        return split_packages

    def _run_worker(self, task: Dict[str, Any]) -> Dict[str, Any]:
        started_at = time.time()
        cmd = self._build_restler_command(task["task_dict_path"], task["task_grammar_path"])
        stdout_path = os.path.join(task["worker_dir"], "worker_stdout.txt")
        stderr_path = os.path.join(task["worker_dir"], "worker_stderr.txt")
        env = os.environ.copy()
        env["PYTHONPATH"] = self._prepend_path(env.get("PYTHONPATH", ""), self.project_root)

        with open(stdout_path, "w", encoding="utf-8", errors="ignore") as stdout, open(
            stderr_path, "w", encoding="utf-8", errors="ignore"
        ) as stderr:
            try:
                completed = subprocess.run(
                    cmd,
                    cwd=task["worker_dir"],
                    stdout=stdout,
                    stderr=stderr,
                    timeout=self._resolve_task_timeout_sec(),
                    env=env,
                )
                returncode = completed.returncode
                error = None
            except subprocess.TimeoutExpired as exc:
                returncode = -1
                error = f"timeout after {self._resolve_task_timeout_sec()} seconds"
                self._append_text(stderr_path, f"\n[ParallelTaskScheduler] {error}: {exc}\n")

        finished_at = time.time()
        result = {
            "task_id": task["task_id"],
            "status": "completed" if returncode == 0 else "failed",
            "returncode": returncode,
            "error": error,
            "started_at": self._timestamp(started_at),
            "finished_at": self._timestamp(finished_at),
            "duration_sec": round(finished_at - started_at, 3),
            "worker_dir": task["worker_dir"],
            "command": cmd,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "task_package": task["task_package"],
            "grammar_summary": task["grammar_summary"],
            "grammar_patch_summary": task.get("grammar_patch_summary", {}),
            "dictionary_seed_summary": task.get("dictionary_seed_summary", {}),
            "artifacts": self._collect_worker_artifacts(task["worker_dir"]),
        }
        self._write_json(os.path.join(task["worker_dir"], "worker_result.json"), result)
        return result

    def _build_restler_command(self, dict_path: str, grammar_path: str) -> List[str]:
        cmd = [
            self.restler_exe,
            self.restler_mode,
            "--grammar_file",
            grammar_path,
            "--dictionary_file",
            dict_path,
        ]
        if self.no_ssl:
            cmd.append("--no_ssl")
        cmd.extend(["--settings", self.settings_path])
        if self.host:
            cmd.extend(["--host", self.host])
        if self.target_ip:
            cmd.extend(["--target_ip", self.target_ip])
        if self.target_port is not None:
            cmd.extend(["--target_port", str(self.target_port)])
        if self.restler_mode in {"fuzz", "fuzz-lean"}:
            if self.time_budget is not None:
                cmd.extend(["--time_budget", str(self.time_budget)])
            if self.restler_mode == "fuzz" and self.search_strategy:
                cmd.extend(["--search_strategy", self.search_strategy])
        return cmd

    def _collect_worker_artifacts(self, worker_dir: str) -> Dict[str, Any]:
        output_root = os.path.join(worker_dir, self._restler_output_folder())
        artifacts = {
            "output_root": output_root if os.path.exists(output_root) else None,
            "testing_summary_path": self._find_latest_file(worker_dir, "testing_summary.json"),
            "speccov_path": self._find_latest_file(worker_dir, "speccov.json"),
            "error_summary_path": self._find_latest_file(worker_dir, "restler_error_summary.jsonl"),
            "bugs_path": self._find_latest_file(worker_dir, "Bugs.json"),
            "network_logs": self._find_files(worker_dir, r"network\..*\.txt$"),
        }
        artifacts["testing_summary"] = self._load_json_if_exists(artifacts["testing_summary_path"])
        artifacts["speccov_summary"] = self._summarize_speccov(artifacts["speccov_path"])
        artifacts["error_summary_count"] = self._count_jsonl_lines(artifacts["error_summary_path"])
        artifacts["bug_summary"] = self._summarize_bugs(artifacts["bugs_path"])
        artifacts["resource_ids"] = self._extract_resource_ids(artifacts["network_logs"])
        return artifacts

    def _seed_task_dictionary(
        self,
        dict_path: str,
        task_package: Dict[str, Any],
        grammar_writer: TaskGrammarWriter,
        resource_pool: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not self.seed_workers_from_resource_pool or not resource_pool:
            return {
                "enabled": self.seed_workers_from_resource_pool,
                "seeded_value_count": 0,
                "resource_keys": [],
                "parameter_names": [],
            }

        resources = resource_pool.get("resources", {}) if isinstance(resource_pool, dict) else {}
        if not isinstance(resources, dict) or not resources:
            return {
                "enabled": self.seed_workers_from_resource_pool,
                "seeded_value_count": 0,
                "resource_keys": [],
                "parameter_names": [],
            }

        operation_ids = list(task_package.get("operation_ids", []))
        operation_blocks = {
            block["operation_id"]: block
            for block in grammar_writer.blocks
        }
        path_param_names: Set[str] = set()
        for operation_id in operation_ids:
            block = operation_blocks.get(operation_id)
            if not block:
                continue
            for param_name in block.get("path_params", []):
                path_param_names.add(str(param_name))

        resource_keys = self._resource_keys_for_task(task_package)
        values_by_parameter: Dict[str, List[Any]] = {}
        all_candidate_values: List[Any] = []
        seeded_parameter_names: Set[str] = set()
        for resource_key in resource_keys:
            resource_entry = resources.get(resource_key)
            if not isinstance(resource_entry, dict):
                continue
            values = resource_entry.get("ids", [])
            if isinstance(values, list):
                filtered_values = self._filter_resource_ids(resource_key, values)
                if not filtered_values:
                    continue
                all_candidate_values.extend(filtered_values)
                parameter_names_for_resource = {str(item) for item in RESOURCE_PARAM_HINTS.get(resource_key, [])}
                if resource_key in task_package.get("shared_state_keys", []):
                    parameter_names_for_resource.update(path_param_names)
                for parameter in parameter_names_for_resource:
                    seeded_parameter_names.add(parameter)
                    values_by_parameter.setdefault(parameter, [])
                    self._extend_list(values_by_parameter[parameter], filtered_values)

        all_candidate_values = self._dedupe_values(all_candidate_values)
        if not all_candidate_values:
            return {
                "enabled": self.seed_workers_from_resource_pool,
                "seeded_value_count": 0,
                "resource_keys": resource_keys,
                "parameter_names": sorted(path_param_names),
            }

        dictionary = self._load_json(dict_path)
        for key, default in [
            ("restler_fuzzable_int", []),
            ("restler_fuzzable_string", []),
            ("restler_fuzzable_string_unquoted", []),
            ("restler_custom_payload", {}),
            ("restler_custom_payload_unquoted", {}),
            ("restler_custom_payload_query", {}),
            ("restler_custom_payload_query_unquoted", {}),
        ]:
            if key not in dictionary or not isinstance(dictionary[key], type(default)):
                dictionary[key] = default.copy() if isinstance(default, dict) else list(default)

        numeric_values = [str(int(value)) for value in all_candidate_values if self._looks_like_int(value)]
        string_values = [str(value) for value in all_candidate_values]
        self._extend_list(dictionary["restler_fuzzable_int"], numeric_values)
        self._extend_list(dictionary["restler_fuzzable_string"], string_values)
        self._extend_list(dictionary["restler_fuzzable_string_unquoted"], string_values)

        parameter_names = sorted(seeded_parameter_names or path_param_names)
        for parameter in parameter_names:
            parameter_values = [str(value) for value in values_by_parameter.get(parameter, [])]
            if not parameter_values:
                continue
            for key in [
                "restler_custom_payload",
                "restler_custom_payload_unquoted",
                "restler_custom_payload_query",
                "restler_custom_payload_query_unquoted",
            ]:
                dictionary[key].setdefault(parameter, [])
                self._prepend_list(dictionary[key][parameter], parameter_values)

        self._write_json(dict_path, dictionary)
        return {
            "enabled": True,
            "seeded_value_count": len(all_candidate_values),
            "resource_keys": resource_keys,
            "parameter_names": parameter_names,
            "numeric_value_count": len(numeric_values),
        }

    def _seed_task_semantics(
        self,
        dict_path: str,
        task_package: Dict[str, Any],
    ) -> Dict[str, Any]:
        operation_ids = [str(item) for item in task_package.get("operation_ids", [])]
        resource_keys = [str(item) for item in task_package.get("shared_state_keys", [])]
        if not self.seed_workers_from_semantics or not self.semantic_constraints_paths:
            return {
                "enabled": self.seed_workers_from_semantics,
                "semantic_constraint_paths": self.semantic_constraints_paths,
                "semantic_constraint_count": 0,
                "seeded_parameter_count": 0,
                "operation_scope": operation_ids,
                "resource_scope": resource_keys,
            }

        seeder = FeedbackSeeder(
            min_confidence=self.semantic_seed_min_confidence,
            include_wordpress_defaults=True,
            prune_endpoint_query_payloads=False,
            include_endpoint_query_semantics=True,
        )
        summary = seeder.seed(
            base_dict_path=dict_path,
            output_dict_path=dict_path,
            semantic_constraints_paths=self.semantic_constraints_paths,
            resource_pool_paths=[],
            task_plan_path=self.task_plan_path,
            operation_ids=operation_ids,
            resource_keys=resource_keys,
        )
        summary.update(
            {
                "enabled": True,
                "semantic_constraint_paths": self.semantic_constraints_paths,
                "seeded_parameter_count": len(summary.get("seeded_parameters", {})),
            }
        )
        return summary

    def _patch_task_grammar(self, grammar_path: str, task_package: Dict[str, Any]) -> Dict[str, Any]:
        operation_ids = [str(item) for item in task_package.get("operation_ids", [])]
        if not self.seed_workers_from_semantics or not self.semantic_constraints_paths:
            return {
                "enabled": self.seed_workers_from_semantics,
                "patched": False,
                "body_fields_added": [],
                "query_fields_added": [],
                "query_primitives_replaced": [],
            }
        patcher = TaskSemanticGrammarPatcher(
            constraints_paths=self.semantic_constraints_paths,
            min_confidence=self.semantic_seed_min_confidence,
        )
        return patcher.patch(grammar_path, operation_ids)

    def _patch_resource_path_primitives(
        self,
        grammar_path: str,
        task_package: Dict[str, Any],
        seeded_parameter_names: List[str],
    ) -> Dict[str, Any]:
        seeded = {str(item) for item in seeded_parameter_names}
        if not seeded:
            return {"patched": False, "path_primitives_replaced": []}

        with open(grammar_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        endpoint_pattern = re.compile(
            r"^\s*#\s*Endpoint:\s*(?P<endpoint>.+?),\s*method:\s*(?P<method>[A-Za-z]+)\s*$"
        )
        patched_lines: List[str] = []
        block: List[str] = []
        current_operation: Optional[str] = None
        current_path_params: List[str] = []
        replacements: List[Dict[str, str]] = []

        def flush_block() -> None:
            nonlocal block, current_operation, current_path_params
            if not block:
                return
            patched_block = block
            for parameter in current_path_params:
                if parameter not in seeded:
                    continue
                patched_block, changed = self._replace_one_path_primitive(patched_block, parameter)
                if changed:
                    replacements.append({"operation_id": current_operation or "", "parameter": parameter})
            patched_lines.extend(patched_block)
            block = []

        for line in lines:
            match = endpoint_pattern.match(line)
            if match:
                flush_block()
                endpoint = match.group("endpoint").strip()
                current_operation = f"{match.group('method').strip().upper()} {endpoint}"
                current_path_params = [str(item) for item in re.findall(r"\{([^{}]+)\}", endpoint)]
                block = [line]
                continue
            if current_operation:
                block.append(line)
            else:
                patched_lines.append(line)
        flush_block()

        if replacements:
            with open(grammar_path, "w", encoding="utf-8") as f:
                f.writelines(patched_lines)

        return {"patched": bool(replacements), "path_primitives_replaced": replacements}

    def _replace_one_path_primitive(self, lines: List[str], parameter: str) -> Tuple[List[str], bool]:
        result = list(lines)
        for index in range(1, len(result)):
            if 'primitives.restler_static_string("/")' not in result[index - 1]:
                continue
            if "primitives.restler_fuzzable_int(" not in result[index]:
                continue
            indent = result[index][: len(result[index]) - len(result[index].lstrip())]
            comma = "," if result[index].rstrip().endswith(",") else ""
            result[index] = f'{indent}primitives.restler_custom_payload("{parameter}", quoted=False){comma}\n'
            return result, True
        return result, False

    def _resource_keys_for_task(self, task_package: Dict[str, Any]) -> List[str]:
        keys = [str(item) for item in task_package.get("shared_state_keys", [])]
        operation_ids = [str(item) for item in task_package.get("operation_ids", [])]
        if any(item.startswith("POST /wp-json/wp/v2/comments") for item in operation_ids):
            keys.append("/wp-json/wp/v2/comment_hosts")
        return self._dedupe_strings(keys)

    def _summarize(self, run_dir: str, started_at: float, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        finished_at = time.time()
        completed = [result for result in results if result.get("status") == "completed"]
        failed = [result for result in results if result.get("status") != "completed"]
        total_valid_endpoints = 0
        total_endpoint_count = 0
        total_errors = 0
        total_bugs = 0

        for result in results:
            artifacts = result.get("artifacts", {})
            speccov_summary = artifacts.get("speccov_summary", {})
            bug_summary = artifacts.get("bug_summary", {})
            total_valid_endpoints += int(speccov_summary.get("valid_endpoint_count", 0) or 0)
            total_endpoint_count += int(speccov_summary.get("endpoint_total", 0) or 0)
            total_errors += int(artifacts.get("error_summary_count", 0) or 0)
            total_bugs += int(bug_summary.get("total_bug_buckets", 0) or 0)

        return {
            "schema_version": 1,
            "run_dir": run_dir,
            "started_at": self._timestamp(started_at),
            "finished_at": self._timestamp(finished_at),
            "duration_sec": round(finished_at - started_at, 3),
            "restler_mode": self.restler_mode,
            "max_workers": self.max_workers,
            "task_count": len(results),
            "completed_task_count": len(completed),
            "failed_task_count": len(failed),
            "aggregate": {
                "endpoint_total_sum": total_endpoint_count,
                "valid_endpoint_count_sum": total_valid_endpoints,
                "error_summary_count_sum": total_errors,
                "bug_bucket_count_sum": total_bugs,
            },
            "results": sorted(results, key=lambda item: item.get("task_id", "")),
        }

    def _load_resource_pool(self, path: Optional[str]) -> Dict[str, Any]:
        if path and os.path.exists(path):
            data = self._load_json_if_exists(path)
            if isinstance(data, dict):
                return self._normalize_resource_pool(data)
        return {
            "schema_version": 1,
            "resource_count": 0,
            "resources": {},
            "note": "Shared resource pool for parallel RESTler workers.",
        }

    def _write_resource_pool(self, pool: Dict[str, Any], path: str) -> None:
        normalized = self._normalize_resource_pool(pool)
        self._write_json(path, normalized)

    def _normalize_resource_pool(self, pool: Dict[str, Any]) -> Dict[str, Any]:
        resources = pool.get("resources", {}) if isinstance(pool, dict) else {}
        normalized_resources: Dict[str, Dict[str, Any]] = {}
        if isinstance(resources, dict):
            for resource_key, entry in resources.items():
                if not isinstance(entry, dict):
                    continue
                ids = self._dedupe_values(entry.get("ids", []))
                sources = sorted({str(item) for item in entry.get("sources", []) if item is not None})
                normalized_resources[str(resource_key)] = {
                    "ids": ids,
                    "sources": sources,
                }
        return {
            "schema_version": 1,
            "resource_count": len(normalized_resources),
            "resources": dict(sorted(normalized_resources.items())),
            "note": pool.get("note", "Shared resource pool for parallel RESTler workers.") if isinstance(pool, dict) else "",
        }

    def _merge_resource_pools(self, base_pool: Dict[str, Any], update_pool: Dict[str, Any]) -> Dict[str, Any]:
        merged = self._normalize_resource_pool(base_pool)
        update = self._normalize_resource_pool(update_pool)
        resources = merged.setdefault("resources", {})
        for resource_key, update_entry in update.get("resources", {}).items():
            entry = resources.setdefault(resource_key, {"ids": [], "sources": []})
            self._extend_list(entry["ids"], update_entry.get("ids", []))
            self._extend_list(entry["sources"], update_entry.get("sources", []))
            entry["ids"] = self._dedupe_values(entry["ids"])
            entry["sources"] = sorted({str(item) for item in entry["sources"] if item is not None})
        merged["resource_count"] = len(resources)
        return merged

    def _build_resource_pool(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        pool: Dict[str, Dict[str, Any]] = {}
        for result in results:
            shared_state_keys = result.get("task_package", {}).get("shared_state_keys", [])
            resource_ids = result.get("artifacts", {}).get("resource_ids", [])
            for resource_key in shared_state_keys:
                entry = pool.setdefault(resource_key, {"ids": [], "sources": []})
                for resource_id in resource_ids:
                    if resource_id not in entry["ids"]:
                        entry["ids"].append(resource_id)
                if resource_ids:
                    entry["sources"].append(result.get("task_id"))

        for entry in pool.values():
            entry["ids"] = sorted(entry["ids"], key=lambda item: (len(str(item)), str(item)))
            entry["sources"] = sorted(set(entry["sources"]))

        return {
            "schema_version": 1,
            "resource_count": len(pool),
            "resources": pool,
            "note": "Resource ids are extracted from worker network logs and can seed later dictionary updates.",
        }

    def _dedupe_values(self, values: Any) -> List[Any]:
        if not isinstance(values, list):
            return []
        seen = set()
        result: List[Any] = []
        for value in values:
            normalized = str(value)
            if normalized in seen:
                continue
            seen.add(normalized)
            if self._looks_like_int(value):
                result.append(int(value))
            else:
                result.append(str(value))
        return sorted(result, key=lambda item: (len(str(item)), str(item)))

    def _filter_resource_ids(self, resource_key: str, ids: List[Any]) -> List[Any]:
        protected = PROTECTED_WORDPRESS_RESOURCE_IDS.get(str(resource_key), set())
        return [item for item in ids if str(item) not in protected]

    def _extend_list(self, target: List[Any], values: List[Any]) -> None:
        existing = {str(item) for item in target}
        for value in values:
            if str(value) in existing:
                continue
            target.append(value)
            existing.add(str(value))

    def _prepend_list(self, target: List[Any], values: List[Any]) -> None:
        prefix: List[Any] = []
        for value in values:
            if str(value) in {str(item) for item in prefix}:
                continue
            prefix.append(value)
        if prefix:
            prefix_values = {str(item) for item in prefix}
            target[:] = prefix + [item for item in target if str(item) not in prefix_values]

    def _dedupe_strings(self, values: List[Any]) -> List[str]:
        seen = set()
        result: List[str] = []
        for value in values:
            text = str(value)
            if text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _looks_like_int(self, value: Any) -> bool:
        return re.fullmatch(r"-?\d+", str(value)) is not None

    def _summarize_speccov(self, path: Optional[str]) -> Dict[str, Any]:
        if not path or not os.path.exists(path):
            return {
                "available": False,
                "endpoint_total": 0,
                "valid_endpoint_count": 0,
                "invalid_endpoint_count": 0,
            }
        data = self._load_json_if_exists(path) or {}
        endpoint_total = 0
        valid_endpoint_count = 0
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            endpoint_total += 1
            status_code = str(entry.get("status_code", ""))
            if status_code.startswith("2") or status_code.startswith("5"):
                valid_endpoint_count += 1
        return {
            "available": True,
            "endpoint_total": endpoint_total,
            "valid_endpoint_count": valid_endpoint_count,
            "invalid_endpoint_count": max(0, endpoint_total - valid_endpoint_count),
        }

    def _summarize_bugs(self, path: Optional[str]) -> Dict[str, Any]:
        if not path or not os.path.exists(path):
            return {"available": False, "total_bug_buckets": 0}
        data = self._load_json_if_exists(path) or {}
        bugs = data.get("bugs", []) if isinstance(data, dict) else []
        return {
            "available": True,
            "total_bug_buckets": len(bugs) if isinstance(bugs, list) else 0,
        }

    def _extract_resource_ids(self, network_logs: List[str]) -> List[Any]:
        ids: Set[Any] = set()
        patterns = [
            re.compile(r'"id"\s*:\s*(\d+)'),
            re.compile(r'"id"\s*:\s*"([^"]{1,128})"'),
        ]
        for path in network_logs:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except Exception:
                continue
            for pattern in patterns:
                for match in pattern.finditer(text):
                    raw_value = match.group(1)
                    if str(raw_value).isdigit():
                        ids.add(int(raw_value))
                    else:
                        ids.add(raw_value)
        return sorted(ids, key=lambda item: (len(str(item)), str(item)))

    def _failed_task_result(self, task: Dict[str, Any], error: str) -> Dict[str, Any]:
        result = {
            "task_id": task["task_id"],
            "status": "failed",
            "returncode": None,
            "error": error,
            "worker_dir": task["worker_dir"],
            "task_package": task["task_package"],
            "grammar_summary": task["grammar_summary"],
            "grammar_patch_summary": task.get("grammar_patch_summary", {}),
            "dictionary_seed_summary": task.get("dictionary_seed_summary", {}),
            "artifacts": {},
        }
        self._write_json(os.path.join(task["worker_dir"], "worker_result.json"), result)
        return result

    def _resolve_task_timeout_sec(self) -> Optional[int]:
        if self.task_timeout_sec is not None:
            return int(self.task_timeout_sec)
        if self.restler_mode == "test":
            return 1200
        if self.restler_mode in {"fuzz", "fuzz-lean"} and self.time_budget is not None:
            return int(self.time_budget * 3600) + 300
        if self.restler_mode == "fuzz-lean":
            return 1800
        return 3600

    def _restler_output_folder(self) -> str:
        if self.restler_mode == "fuzz-lean":
            return "FuzzLean"
        if self.restler_mode == "fuzz":
            return "Fuzz"
        return "Test"

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.project_root, path)

    def _load_json(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_json_if_exists(self, path: Optional[str]) -> Optional[Dict[str, Any]]:
        if not path or not os.path.exists(path):
            return None
        try:
            return self._load_json(path)
        except Exception:
            return None

    def _write_json(self, path: str, data: Dict[str, Any]) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _count_jsonl_lines(self, path: Optional[str]) -> int:
        if not path or not os.path.exists(path):
            return 0
        count = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    def _find_latest_file(self, root_dir: str, filename: str) -> Optional[str]:
        matches = []
        for root, _, files in os.walk(root_dir):
            if filename in files:
                matches.append(os.path.join(root, filename))
        if not matches:
            return None
        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    def _find_files(self, root_dir: str, filename_regex: str) -> List[str]:
        pattern = re.compile(filename_regex)
        matches = []
        for root, _, files in os.walk(root_dir):
            for filename in files:
                if pattern.search(filename):
                    matches.append(os.path.join(root, filename))
        matches.sort()
        return matches

    def _prepend_path(self, current: str, path: str) -> str:
        if not current:
            return path
        return path + os.pathsep + current

    def _append_text(self, path: str, text: str) -> None:
        with open(path, "a", encoding="utf-8", errors="ignore") as f:
            f.write(text)

    def _safe_name(self, value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
        return safe or "task"

    def _timestamp(self, timestamp: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run dependency-graph task packages with parallel RESTler workers.")
    parser.add_argument("--project_root", default=".", help="RESTler working directory.")
    parser.add_argument("--task_plan", required=True, help="task_plan.json generated by operation_dependency_graph.py.")
    parser.add_argument("--grammar", required=True, help="Enhanced RESTler grammar used as the source for task grammars.")
    parser.add_argument("--dictionary", required=True, help="RESTler dictionary used by all workers.")
    parser.add_argument("--settings", default="engine_settings.json", help="RESTler settings file.")
    parser.add_argument("--output_dir", default="parallel_runs", help="Directory for parallel run artifacts.")
    parser.add_argument("--mode", "--restler_mode", dest="restler_mode", choices=sorted(SUPPORTED_MODES), default="test")
    parser.add_argument("--max_workers", type=int, default=2, help="Maximum RESTler worker processes.")
    parser.add_argument("--no_ssl", action="store_true", default=True, help="Disable SSL when talking to the target service.")
    parser.add_argument("--ssl", dest="no_ssl", action="store_false", help="Enable SSL when talking to the target service.")
    parser.add_argument("--time_budget", type=float, default=None, help="Fuzz time budget in hours for each worker.")
    parser.add_argument("--search_strategy", choices=["bfs-fast", "bfs", "bfs-cheap", "random-walk"], default=None)
    parser.add_argument("--host", default=None, help="Override the Host header.")
    parser.add_argument("--target_ip", default=None, help="Override the target IP.")
    parser.add_argument("--target_port", type=int, default=None, help="Override the target port.")
    parser.add_argument("--task_timeout_sec", type=int, default=None, help="Timeout for each worker.")
    parser.add_argument("--fail_fast", action="store_true", help="Stop the scheduler when any worker raises before RESTler returns.")
    parser.add_argument("--limit_tasks", type=int, default=None, help="Run only the first N task packages by priority.")
    parser.add_argument("--prepare_only", action="store_true", help="Create worker directories and task grammars without running RESTler.")
    parser.add_argument("--resource_pool", default=None, help="Optional shared resource_pool.json used to seed workers and store discovered ids.")
    parser.add_argument("--disable_resource_pool_seed", action="store_true", help="Do not inject shared resource ids into worker dictionaries.")
    parser.add_argument("--semantic_constraints", nargs="*", default=[], help="semantic_constraints.jsonl files used for task-local dictionary seeding.")
    parser.add_argument("--disable_semantic_seed", action="store_true", help="Do not inject semantic feedback into worker dictionaries.")
    parser.add_argument("--semantic_seed_min_confidence", type=float, default=0.60, help="Minimum confidence for semantic feedback seeding.")
    parser.add_argument("--enable_resource_path_patch", action="store_true", help="Experimentally replace path id primitives with resource-pool custom payloads.")
    parser.add_argument("--defer_delete_operations", action="store_true", help="Split DELETE endpoints into low-priority tasks so read/update endpoints run before destructive calls.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    scheduler = ParallelTaskScheduler(
        project_root=args.project_root,
        task_plan_path=args.task_plan,
        grammar_path=args.grammar,
        dict_path=args.dictionary,
        settings_path=args.settings,
        output_dir=args.output_dir,
        restler_mode=args.restler_mode,
        max_workers=args.max_workers,
        no_ssl=args.no_ssl,
        time_budget=args.time_budget,
        search_strategy=args.search_strategy,
        host=args.host,
        target_ip=args.target_ip,
        target_port=args.target_port,
        task_timeout_sec=args.task_timeout_sec,
        continue_on_worker_failure=not args.fail_fast,
        limit_tasks=args.limit_tasks,
        prepare_only=args.prepare_only,
        resource_pool_path=args.resource_pool,
        seed_workers_from_resource_pool=not args.disable_resource_pool_seed,
        semantic_constraints_paths=args.semantic_constraints,
        seed_workers_from_semantics=not args.disable_semantic_seed,
        semantic_seed_min_confidence=args.semantic_seed_min_confidence,
        enable_resource_path_patch=args.enable_resource_path_patch,
        defer_delete_operations=args.defer_delete_operations,
    )
    summary = scheduler.run()
    print(
        "[ParallelTaskScheduler] "
        f"run_dir={summary['run_dir']} "
        f"completed={summary['completed_task_count']}/{summary['task_count']} "
        f"failed={summary['failed_task_count']}"
    )


if __name__ == "__main__":
    main()
