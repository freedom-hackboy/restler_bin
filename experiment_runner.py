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


DEFAULT_VARIANTS = [
    "baseline",
    "semantic",
    "semantic_rule_only",
    "semantic_parallel",
    "parallel_no_resource_seed",
]


class ExperimentRunner:
    def __init__(
        self,
        project_root: str = ".",
        output_dir: str = "experiments",
        variants: Optional[List[str]] = None,
        rounds: int = 3,
        restler_mode: str = "test",
        max_parallel_workers: int = 2,
        restler_timeout_sec: Optional[int] = None,
        parallel_task_timeout_sec: Optional[int] = None,
        time_budget: Optional[float] = None,
        prepare_parallel_only: bool = False,
        dry_run: bool = False,
    ):
        self.project_root = os.path.abspath(project_root)
        self.output_dir = self._resolve_path(output_dir)
        self.variants = variants or list(DEFAULT_VARIANTS)
        self.rounds = rounds
        self.restler_mode = restler_mode
        self.max_parallel_workers = max_parallel_workers
        self.restler_timeout_sec = restler_timeout_sec
        self.parallel_task_timeout_sec = parallel_task_timeout_sec
        self.time_budget = time_budget
        self.prepare_parallel_only = prepare_parallel_only
        self.dry_run = dry_run

    def run(self) -> Dict[str, Any]:
        run_id = time.strftime("experiment-%Y%m%d-%H%M%S")
        run_dir = os.path.join(self.output_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)

        results = []
        for variant in self.variants:
            result = self._run_variant(run_dir, variant)
            results.append(result)
            self._write_json(os.path.join(run_dir, "experiment_summary.partial.json"), self._build_summary(run_dir, results))

        summary = self._build_summary(run_dir, results)
        self._write_json(os.path.join(run_dir, "experiment_summary.json"), summary)
        self._write_csv(os.path.join(run_dir, "experiment_summary.csv"), summary["rows"])
        return summary

    def _run_variant(self, run_dir: str, variant: str) -> Dict[str, Any]:
        variant_dir = os.path.join(run_dir, variant)
        os.makedirs(variant_dir, exist_ok=True)
        cmd = self._build_variant_command(variant, variant_dir)
        self._write_json(os.path.join(variant_dir, "command.json"), {"variant": variant, "command": cmd})

        started_at = time.time()
        if self.dry_run:
            return {
                "variant": variant,
                "status": "dry_run",
                "returncode": None,
                "command": cmd,
                "variant_dir": variant_dir,
                "duration_sec": 0,
                "metrics": {},
            }

        stdout_path = os.path.join(variant_dir, "stdout.txt")
        stderr_path = os.path.join(variant_dir, "stderr.txt")
        with open(stdout_path, "w", encoding="utf-8", errors="ignore") as stdout, open(
            stderr_path, "w", encoding="utf-8", errors="ignore"
        ) as stderr:
            completed = subprocess.run(
                cmd,
                cwd=self.project_root,
                stdout=stdout,
                stderr=stderr,
            )
        finished_at = time.time()

        self._archive_pipeline_outputs(variant_dir)
        metrics = self._collect_variant_metrics(variant_dir)
        return {
            "variant": variant,
            "status": "completed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "command": cmd,
            "variant_dir": variant_dir,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "duration_sec": round(finished_at - started_at, 3),
            "metrics": metrics,
        }

    def _build_variant_command(self, variant: str, variant_dir: str) -> List[str]:
        rounds_dir = os.path.join(variant_dir, "rounds")
        dict_dir = os.path.join(variant_dir, "dict")
        grammar_dir = os.path.join(variant_dir, "grammar")
        parallel_dir = os.path.join(variant_dir, "parallel_runs")
        os.makedirs(rounds_dir, exist_ok=True)
        os.makedirs(dict_dir, exist_ok=True)
        os.makedirs(grammar_dir, exist_ok=True)

        cmd = [
            "python",
            os.path.join(self.project_root, "iterative_pipeline.py"),
            "--project_root",
            self.project_root,
            "--rounds",
            str(self.rounds if variant != "baseline" else 1),
            "--mode",
            self.restler_mode,
            "--constraint_memory",
            os.path.join(variant_dir, "constraint_memory.jsonl"),
            "--parallel_output_dir",
            parallel_dir,
            "--parallel_resource_pool",
            os.path.join(variant_dir, "resource_pool.json"),
        ]

        if self.restler_timeout_sec is not None:
            cmd.extend(["--restler_timeout_sec", str(self.restler_timeout_sec)])
        if self.parallel_task_timeout_sec is not None:
            cmd.extend(["--parallel_task_timeout_sec", str(self.parallel_task_timeout_sec)])
        if self.time_budget is not None:
            cmd.extend(["--time_budget", str(self.time_budget)])

        if variant == "baseline":
            cmd.extend(["--disable_llm_extraction", "--disable_regression_rollback"])
        elif variant == "semantic_rule_only":
            cmd.append("--disable_llm_extraction")
        elif variant == "semantic_parallel":
            cmd.extend(["--run_parallel_workers", "--max_parallel_workers", str(self.max_parallel_workers)])
        elif variant == "parallel_no_resource_seed":
            cmd.extend([
                "--run_parallel_workers",
                "--max_parallel_workers",
                str(self.max_parallel_workers),
                "--disable_parallel_resource_pool_seed",
            ])
        elif variant != "semantic":
            raise ValueError(f"Unknown experiment variant: {variant}")

        if self.prepare_parallel_only and variant in {"semantic_parallel", "parallel_no_resource_seed"}:
            cmd.append("--parallel_prepare_only")

        return cmd

    def _collect_variant_metrics(self, variant_dir: str) -> Dict[str, Any]:
        latest_stats = self._find_latest_file(variant_dir, "stats.json")
        latest_parallel_summary = self._find_latest_file(variant_dir, "summary.json")
        stats = self._load_json_if_exists(latest_stats) or {}
        parallel_summary = self._load_json_if_exists(latest_parallel_summary) or {}

        experiment_metrics = stats.get("experiment_metrics", {})
        prtt = experiment_metrics.get("PRTT", {})
        bugs = experiment_metrics.get("Bugs", {})
        dependency_graph_summary = stats.get("dependency_graph_summary", {})
        parallel_stats = stats.get("parallel_summary") or parallel_summary

        return {
            "stats_path": latest_stats,
            "parallel_summary_path": latest_parallel_summary,
            "valid_endpoint_count": stats.get("valid_endpoint_count"),
            "endpoint_total": stats.get("endpoint_total"),
            "constraint_count": stats.get("constraint_count"),
            "applied_constraint_count": stats.get("applied_constraint_count"),
            "prtt_percent": prtt.get("percent"),
            "bug_bucket_count": bugs.get("total_bug_buckets"),
            "dependency_edge_count": dependency_graph_summary.get("edge_count"),
            "task_package_count": dependency_graph_summary.get("task_package_count"),
            "parallel_completed_tasks": parallel_stats.get("completed_task_count") if isinstance(parallel_stats, dict) else None,
            "parallel_failed_tasks": parallel_stats.get("failed_task_count") if isinstance(parallel_stats, dict) else None,
        }

    def _archive_pipeline_outputs(self, variant_dir: str) -> None:
        for name in ["rounds", "dict", "grammar"]:
            source = os.path.join(self.project_root, name)
            target = os.path.join(variant_dir, name)
            if not os.path.exists(source):
                continue
            if os.path.exists(target):
                shutil.rmtree(target)
            shutil.copytree(source, target)

        for filename in ["constraint_memory.jsonl"]:
            source = os.path.join(self.project_root, filename)
            if os.path.exists(source):
                shutil.copy2(source, os.path.join(variant_dir, filename))

    def _build_summary(self, run_dir: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        rows = []
        for result in results:
            metrics = result.get("metrics", {})
            rows.append(
                {
                    "variant": result.get("variant"),
                    "status": result.get("status"),
                    "duration_sec": result.get("duration_sec"),
                    "valid_endpoint_count": metrics.get("valid_endpoint_count"),
                    "endpoint_total": metrics.get("endpoint_total"),
                    "constraint_count": metrics.get("constraint_count"),
                    "applied_constraint_count": metrics.get("applied_constraint_count"),
                    "prtt_percent": metrics.get("prtt_percent"),
                    "bug_bucket_count": metrics.get("bug_bucket_count"),
                    "dependency_edge_count": metrics.get("dependency_edge_count"),
                    "task_package_count": metrics.get("task_package_count"),
                    "parallel_completed_tasks": metrics.get("parallel_completed_tasks"),
                    "parallel_failed_tasks": metrics.get("parallel_failed_tasks"),
                    "variant_dir": result.get("variant_dir"),
                }
            )
        return {
            "schema_version": 1,
            "run_dir": run_dir,
            "variants": self.variants,
            "rows": rows,
            "results": results,
        }

    def _find_latest_file(self, root_dir: str, filename: str) -> Optional[str]:
        matches = []
        for root, _, files in os.walk(root_dir):
            if filename in files:
                matches.append(os.path.join(root, filename))
        if not matches:
            return None
        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

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

    def _write_csv(self, path: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.project_root, path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run RESTler semantic feedback and parallel scheduling experiments.")
    parser.add_argument("--project_root", default=".", help="RESTler working directory.")
    parser.add_argument("--output_dir", default="experiments", help="Experiment artifact directory.")
    parser.add_argument("--variants", nargs="+", choices=DEFAULT_VARIANTS, default=DEFAULT_VARIANTS)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--mode", "--restler_mode", dest="restler_mode", default="test", choices=["test", "fuzz", "fuzz-lean"])
    parser.add_argument("--max_parallel_workers", type=int, default=2)
    parser.add_argument("--restler_timeout_sec", type=int, default=None)
    parser.add_argument("--parallel_task_timeout_sec", type=int, default=None)
    parser.add_argument("--time_budget", type=float, default=None)
    parser.add_argument("--prepare_parallel_only", action="store_true")
    parser.add_argument("--dry_run", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    runner = ExperimentRunner(
        project_root=args.project_root,
        output_dir=args.output_dir,
        variants=args.variants,
        rounds=args.rounds,
        restler_mode=args.restler_mode,
        max_parallel_workers=args.max_parallel_workers,
        restler_timeout_sec=args.restler_timeout_sec,
        parallel_task_timeout_sec=args.parallel_task_timeout_sec,
        time_budget=args.time_budget,
        prepare_parallel_only=args.prepare_parallel_only,
        dry_run=args.dry_run,
    )
    summary = runner.run()
    print(f"[ExperimentRunner] run_dir={summary['run_dir']} variants={len(summary['rows'])}")


if __name__ == "__main__":
    main()
