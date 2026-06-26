#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from iterative_pipeline import IterativePipeline
from operation_dependency_graph import OperationDependencyGraphBuilder
from parallel_task_scheduler import ParallelTaskScheduler


class SemanticParallelPipeline:
    """
    Full closed-loop experiment:
    1. Run semantic-feedback iterative RESTler rounds.
    2. Merge semantic constraints from all rounds.
    3. Build dependency graph and weak-component task plan.
    4. Run parallel workers with task-local semantic seeding and
       resource-pool collaboration.
    5. Replay parallel workers with the discovered resource pool.
    6. Optionally validate the clean final-round grammar/dictionary.
    """

    def __init__(
        self,
        project_root: str = ".",
        output_dir: str = "combined_runs",
        semantic_rounds: int = 3,
        semantic_mode: str = "test",
        parallel_mode: str = "test",
        max_parallel_workers: int = 2,
        parallel_time_budget: Optional[float] = None,
        restler_timeout_sec: Optional[int] = 600,
        parallel_task_timeout_sec: Optional[int] = 600,
        semantic_timeout_sec: int = 300,
        dict_timeout_sec: int = 180,
        grammar_timeout_sec: int = 180,
        prepare_parallel_only: bool = False,
        skip_validation: bool = False,
        disable_llm_extraction: bool = False,
        reset_constraint_memory: bool = False,
        min_feedback_confidence: float = 0.60,
        enable_resource_replay: bool = True,
        bootstrap_wordpress_resources: bool = False,
        wordpress_base_url: str = "http://192.168.65.128:8088",
        enable_resource_path_patch: bool = False,
        defer_delete_operations: bool = False,
    ):
        self.project_root = os.path.abspath(project_root)
        self.output_dir = self._resolve_path(output_dir)
        self.semantic_rounds = semantic_rounds
        self.semantic_mode = semantic_mode
        self.parallel_mode = parallel_mode
        self.max_parallel_workers = max_parallel_workers
        self.parallel_time_budget = parallel_time_budget
        self.restler_timeout_sec = restler_timeout_sec
        self.parallel_task_timeout_sec = parallel_task_timeout_sec
        self.semantic_timeout_sec = semantic_timeout_sec
        self.dict_timeout_sec = dict_timeout_sec
        self.grammar_timeout_sec = grammar_timeout_sec
        self.prepare_parallel_only = prepare_parallel_only
        self.skip_validation = skip_validation
        self.disable_llm_extraction = disable_llm_extraction
        self.reset_constraint_memory = reset_constraint_memory
        self.min_feedback_confidence = min_feedback_confidence
        self.enable_resource_replay = enable_resource_replay
        self.bootstrap_wordpress_resources = bootstrap_wordpress_resources
        self.wordpress_base_url = wordpress_base_url
        self.enable_resource_path_patch = enable_resource_path_patch
        self.defer_delete_operations = defer_delete_operations

        self.run_id = time.strftime("semantic-parallel-%Y%m%d-%H%M%S")
        self.run_dir = os.path.join(self.output_dir, self.run_id)
        self.combined_dir = os.path.join(self.run_dir, "combined")
        self.semantic_snapshot_dir = os.path.join(self.run_dir, "semantic_rounds")
        os.makedirs(self.combined_dir, exist_ok=True)
        os.makedirs(self.semantic_snapshot_dir, exist_ok=True)

    def run(self) -> Dict[str, Any]:
        started_at = time.time()
        self._write_stage("semantic_iterations", "running")
        self._run_semantic_iterations()
        self._snapshot_semantic_outputs()

        final_dict = os.path.join(self.project_root, "dict", f"dict_round{self.semantic_rounds}.json")
        final_grammar = os.path.join(self.project_root, "grammar", f"grammar_round{self.semantic_rounds}.py")
        merged_constraints = os.path.join(self.combined_dir, "merged_semantic_constraints.jsonl")
        merged_applied = os.path.join(self.combined_dir, "merged_applied_constraints.jsonl")
        self._merge_jsonl("semantic_constraints.jsonl", merged_constraints)
        self._merge_jsonl("applied_constraints.jsonl", merged_applied)

        local_semantic_seed_info = {
            "enabled": True,
            "scope": "task_package",
            "strategy": "semantic constraints are filtered by worker operation_ids before being injected into each worker dict.json",
            "min_confidence": self.min_feedback_confidence,
            "constraints_path": merged_constraints,
        }

        self._write_stage("dependency_graph", "running")
        graph_path = os.path.join(self.combined_dir, "dependency_graph.json")
        task_plan_path = os.path.join(self.combined_dir, "task_plan.json")
        graph = OperationDependencyGraphBuilder(
            grammar_path=final_grammar,
            constraints_path=merged_constraints,
            applied_constraints_path=merged_applied,
            min_confidence=self.min_feedback_confidence,
        ).write(graph_path, task_plan_path)

        self._write_stage("parallel_workers", "running")
        parallel_output_dir = os.path.join(self.run_dir, "parallel_runs")
        resource_pool_path = os.path.join(self.combined_dir, "resource_pool.json")
        bootstrap_summary: Optional[Dict[str, Any]] = None
        if self.bootstrap_wordpress_resources and not self.prepare_parallel_only:
            self._write_stage("wordpress_resource_bootstrap", "running")
            bootstrap_summary = self._run_wordpress_resource_bootstrap(resource_pool_path)
        parallel_summary = ParallelTaskScheduler(
            project_root=self.project_root,
            task_plan_path=task_plan_path,
            grammar_path=final_grammar,
            dict_path=final_dict,
            settings_path=os.path.join(self.project_root, "engine_settings.json"),
            output_dir=parallel_output_dir,
            restler_mode=self.parallel_mode,
            max_workers=self.max_parallel_workers,
            time_budget=self.parallel_time_budget,
            task_timeout_sec=self.parallel_task_timeout_sec,
            resource_pool_path=resource_pool_path,
            prepare_only=self.prepare_parallel_only,
            semantic_constraints_paths=[merged_constraints],
            seed_workers_from_semantics=True,
            semantic_seed_min_confidence=self.min_feedback_confidence,
            enable_resource_path_patch=self.enable_resource_path_patch,
            defer_delete_operations=self.defer_delete_operations,
        ).run()
        self._write_json(os.path.join(self.combined_dir, "parallel_summary.json"), parallel_summary)

        resource_replay_summary: Optional[Dict[str, Any]] = None
        if self.enable_resource_replay and not self.prepare_parallel_only:
            self._write_stage("parallel_resource_replay", "running")
            resource_replay_output_dir = os.path.join(self.run_dir, "parallel_replay_runs")
            resource_replay_summary = ParallelTaskScheduler(
                project_root=self.project_root,
                task_plan_path=task_plan_path,
                grammar_path=final_grammar,
                dict_path=final_dict,
                settings_path=os.path.join(self.project_root, "engine_settings.json"),
                output_dir=resource_replay_output_dir,
                restler_mode=self.parallel_mode,
                max_workers=self.max_parallel_workers,
                time_budget=self.parallel_time_budget,
                task_timeout_sec=self.parallel_task_timeout_sec,
                resource_pool_path=resource_pool_path,
                prepare_only=False,
                semantic_constraints_paths=[merged_constraints],
                seed_workers_from_semantics=True,
                semantic_seed_min_confidence=self.min_feedback_confidence,
                enable_resource_path_patch=self.enable_resource_path_patch,
                defer_delete_operations=self.defer_delete_operations,
            ).run()
            self._write_json(os.path.join(self.combined_dir, "resource_replay_summary.json"), resource_replay_summary)

        validation_summary: Optional[Dict[str, Any]] = None
        if not self.skip_validation and not self.prepare_parallel_only:
            self._write_stage("final_round_validation", "running")
            validation_summary = self._run_validation(final_dict, final_grammar)

        report = {
            "schema_version": 1,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "duration_sec": round(time.time() - started_at, 3),
            "semantic_rounds": self.semantic_rounds,
            "semantic_mode": self.semantic_mode,
            "parallel_mode": self.parallel_mode,
            "artifacts": {
                "semantic_snapshot_dir": self.semantic_snapshot_dir,
                "merged_constraints": merged_constraints,
                "merged_applied_constraints": merged_applied,
                "final_round_dict": final_dict,
                "final_round_grammar": final_grammar,
                "dependency_graph": graph_path,
                "task_plan": task_plan_path,
                "resource_pool": resource_pool_path,
            },
            "local_semantic_seed_summary": local_semantic_seed_info,
            "bootstrap_summary": bootstrap_summary,
            "dependency_graph_summary": graph.get("summary", {}),
            "parallel_summary": self._compact_parallel_summary(parallel_summary),
            "resource_replay_summary": self._compact_parallel_summary(resource_replay_summary) if resource_replay_summary else None,
            "effective_parallel_summary": self._compact_parallel_summary(resource_replay_summary or parallel_summary),
            "validation_summary": validation_summary,
            "round_endpoint_pass_rates": self._collect_round_pass_rates(),
        }
        self._write_json(os.path.join(self.run_dir, "semantic_parallel_report.json"), report)
        self._write_pass_rate_csv(os.path.join(self.run_dir, "round_endpoint_pass_rates.csv"), report["round_endpoint_pass_rates"])
        self._write_stage("complete", "completed")
        return report

    def _run_semantic_iterations(self) -> None:
        pipeline = IterativePipeline(
            rounds=self.semantic_rounds,
            project_root=self.project_root,
            restler_mode=self.semantic_mode,
            restler_timeout_sec=self.restler_timeout_sec,
            semantic_timeout_sec=self.semantic_timeout_sec,
            dict_timeout_sec=self.dict_timeout_sec,
            grammar_timeout_sec=self.grammar_timeout_sec,
            disable_llm_extraction=self.disable_llm_extraction,
            reset_constraint_memory=self.reset_constraint_memory,
        )
        pipeline.run()

    def _run_wordpress_resource_bootstrap(self, output_path: str) -> Dict[str, Any]:
        script_path = os.path.join(self.project_root, "wordpress_resource_bootstrap.py")
        cmd = [
            "python",
            script_path,
            "--settings",
            os.path.join(self.project_root, "engine_settings.json"),
            "--base_url",
            self.wordpress_base_url,
            "--output",
            output_path,
        ]
        started_at = time.time()
        completed = subprocess.run(
            cmd,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        summary = self._load_json_if_exists(output_path) or {}
        summary.update(
            {
                "returncode": completed.returncode,
                "duration_sec": round(time.time() - started_at, 3),
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
                "output_path": output_path,
            }
        )
        self._write_json(os.path.join(self.combined_dir, "bootstrap_summary.json"), summary)
        if completed.returncode != 0:
            raise RuntimeError(f"WordPress resource bootstrap failed: {completed.stderr.strip()}")
        return summary

    def _run_validation(self, dict_path: str, grammar_path: str) -> Dict[str, Any]:
        validation_dir = os.path.join(self.run_dir, "validation")
        os.makedirs(validation_dir, exist_ok=True)
        restler_exe = os.path.join(self.project_root, "restler", "Restler.exe")
        cmd = [
            restler_exe,
            "test",
            "--grammar_file",
            grammar_path,
            "--dictionary_file",
            dict_path,
            "--no_ssl",
            "--settings",
            os.path.join(self.project_root, "engine_settings.json"),
        ]
        stdout_path = os.path.join(validation_dir, "stdout.txt")
        stderr_path = os.path.join(validation_dir, "stderr.txt")
        started_at = time.time()
        with open(stdout_path, "w", encoding="utf-8", errors="ignore") as stdout, open(stderr_path, "w", encoding="utf-8", errors="ignore") as stderr:
            completed = subprocess.run(
                cmd,
                cwd=validation_dir,
                stdout=stdout,
                stderr=stderr,
                timeout=self.restler_timeout_sec,
            )
        summary = self._collect_restler_run_summary(validation_dir)
        summary.update(
            {
                "returncode": completed.returncode,
                "duration_sec": round(time.time() - started_at, 3),
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "command": cmd,
            }
        )
        self._write_json(os.path.join(validation_dir, "validation_summary.json"), summary)
        return summary

    def _collect_restler_run_summary(self, run_dir: str) -> Dict[str, Any]:
        testing_path = self._find_latest_file(run_dir, "testing_summary.json")
        speccov_path = self._find_latest_file(run_dir, "speccov.json")
        error_path = self._find_latest_file(run_dir, "restler_error_summary.jsonl")
        testing = self._load_json_if_exists(testing_path) or {}
        endpoint_total = 0
        valid_count = 0
        speccov = self._load_json_if_exists(speccov_path) or {}
        for entry in speccov.values():
            if not isinstance(entry, dict):
                continue
            endpoint_total += 1
            status_code = str(entry.get("status_code", ""))
            if status_code.startswith("2") or status_code.startswith("5"):
                valid_count += 1
        return {
            "testing_summary_path": testing_path,
            "speccov_path": speccov_path,
            "error_summary_path": error_path,
            "final_spec_coverage": testing.get("final_spec_coverage"),
            "rendered_requests_valid_status": testing.get("rendered_requests_valid_status"),
            "endpoint_total": endpoint_total,
            "valid_endpoint_count": valid_count,
            "endpoint_pass_rate": (valid_count / endpoint_total) if endpoint_total else None,
            "error_summary_count": self._count_jsonl_lines(error_path),
        }

    def _snapshot_semantic_outputs(self) -> None:
        for name in ["rounds", "dict", "grammar"]:
            source = os.path.join(self.project_root, name)
            target = os.path.join(self.semantic_snapshot_dir, name)
            if os.path.exists(target):
                shutil.rmtree(target)
            if os.path.exists(source):
                shutil.copytree(source, target)
        memory = os.path.join(self.project_root, "constraint_memory.jsonl")
        if os.path.exists(memory):
            shutil.copy2(memory, os.path.join(self.semantic_snapshot_dir, "constraint_memory.jsonl"))

    def _merge_jsonl(self, filename: str, output_path: str) -> int:
        seen = set()
        count = 0
        with open(output_path, "w", encoding="utf-8") as out:
            for round_id in range(self.semantic_rounds):
                path = os.path.join(self.project_root, "rounds", f"round{round_id}", filename)
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line in seen:
                            continue
                        try:
                            json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        seen.add(line)
                        out.write(line + "\n")
                        count += 1
        return count

    def _collect_round_pass_rates(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for round_id in range(self.semantic_rounds):
            round_dir = os.path.join(self.project_root, "rounds", f"round{round_id}")
            stats = self._load_json_if_exists(os.path.join(round_dir, "stats.json")) or {}
            testing = self._load_json_if_exists(os.path.join(round_dir, "testing_summary.json")) or {}
            endpoint_total = int(stats.get("endpoint_total", 0) or 0)
            valid = int(stats.get("valid_endpoint_count", 0) or 0)
            rows.append(
                {
                    "round": round_id,
                    "dict_file": os.path.basename(stats.get("dict_path", "") or ""),
                    "grammar_file": os.path.basename(stats.get("grammar_path", "") or ""),
                    "valid_endpoint_count": valid,
                    "endpoint_total": endpoint_total,
                    "endpoint_pass_rate": round(valid / endpoint_total, 4) if endpoint_total else None,
                    "final_spec_coverage": testing.get("final_spec_coverage"),
                    "rendered_requests_valid_status": testing.get("rendered_requests_valid_status"),
                    "constraint_count": stats.get("constraint_count"),
                    "applied_constraint_count": stats.get("applied_constraint_count"),
                    "error_summary_count": stats.get("error_summary_count"),
                }
            )
        return rows

    def _compact_parallel_summary(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "run_dir": summary.get("run_dir"),
            "restler_mode": summary.get("restler_mode"),
            "duration_sec": summary.get("duration_sec"),
            "task_count": summary.get("task_count"),
            "completed_task_count": summary.get("completed_task_count"),
            "failed_task_count": summary.get("failed_task_count"),
            "aggregate": summary.get("aggregate"),
            "resource_pool": summary.get("resource_pool"),
        }

    def _write_pass_rate_csv(self, path: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def _write_stage(self, step: str, status: str) -> None:
        self._write_json(
            os.path.join(self.run_dir, "pipeline_state.json"),
            {
                "step": step,
                "status": status,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def _find_latest_file(self, root_dir: str, filename: str) -> Optional[str]:
        matches = []
        for root, _, files in os.walk(root_dir):
            if filename in files:
                matches.append(os.path.join(root, filename))
        if not matches:
            return None
        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    def _count_jsonl_lines(self, path: Optional[str]) -> int:
        if not path or not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.strip())

    def _load_json_if_exists(self, path: Optional[str]) -> Optional[Dict[str, Any]]:
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _write_json(self, path: str, data: Dict[str, Any]) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.project_root, path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run semantic-feedback iterations followed by dependency-graph parallel testing.")
    parser.add_argument("--project_root", default=".")
    parser.add_argument("--output_dir", default="combined_runs")
    parser.add_argument("--semantic_rounds", type=int, default=3)
    parser.add_argument("--semantic_mode", default="test", choices=["test", "fuzz", "fuzz-lean"])
    parser.add_argument("--parallel_mode", default="test", choices=["test", "fuzz", "fuzz-lean"])
    parser.add_argument("--max_parallel_workers", type=int, default=2)
    parser.add_argument("--parallel_time_budget", type=float, default=None)
    parser.add_argument("--restler_timeout_sec", type=int, default=600)
    parser.add_argument("--parallel_task_timeout_sec", type=int, default=600)
    parser.add_argument("--semantic_timeout_sec", type=int, default=300)
    parser.add_argument("--dict_timeout_sec", type=int, default=180)
    parser.add_argument("--grammar_timeout_sec", type=int, default=180)
    parser.add_argument("--prepare_parallel_only", action="store_true")
    parser.add_argument("--skip_validation", action="store_true")
    parser.add_argument("--disable_llm_extraction", action="store_true")
    parser.add_argument("--reset_constraint_memory", action="store_true")
    parser.add_argument("--min_feedback_confidence", type=float, default=0.60)
    parser.add_argument("--disable_resource_replay", action="store_true")
    parser.add_argument("--bootstrap_wordpress_resources", action="store_true")
    parser.add_argument("--wordpress_base_url", default="http://192.168.65.128:8088")
    parser.add_argument("--enable_resource_path_patch", action="store_true")
    parser.add_argument("--defer_delete_operations", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    pipeline = SemanticParallelPipeline(
        project_root=args.project_root,
        output_dir=args.output_dir,
        semantic_rounds=args.semantic_rounds,
        semantic_mode=args.semantic_mode,
        parallel_mode=args.parallel_mode,
        max_parallel_workers=args.max_parallel_workers,
        parallel_time_budget=args.parallel_time_budget,
        restler_timeout_sec=args.restler_timeout_sec,
        parallel_task_timeout_sec=args.parallel_task_timeout_sec,
        semantic_timeout_sec=args.semantic_timeout_sec,
        dict_timeout_sec=args.dict_timeout_sec,
        grammar_timeout_sec=args.grammar_timeout_sec,
        prepare_parallel_only=args.prepare_parallel_only,
        skip_validation=args.skip_validation,
        disable_llm_extraction=args.disable_llm_extraction,
        reset_constraint_memory=args.reset_constraint_memory,
        min_feedback_confidence=args.min_feedback_confidence,
        enable_resource_replay=not args.disable_resource_replay,
        bootstrap_wordpress_resources=args.bootstrap_wordpress_resources,
        wordpress_base_url=args.wordpress_base_url,
        enable_resource_path_patch=args.enable_resource_path_patch,
        defer_delete_operations=args.defer_delete_operations,
    )
    report = pipeline.run()
    print(
        "[SemanticParallelPipeline] "
        f"run_dir={report['run_dir']} "
        f"rounds={report['semantic_rounds']} "
        f"parallel_tasks={report['parallel_summary'].get('task_count')}"
    )


if __name__ == "__main__":
    main()
