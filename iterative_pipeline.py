# iterative_pipeline.py
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from knowledge_manager import KnowledgeManager


SUPPORTED_MODES = {"test", "fuzz", "fuzz-lean"}


class IterativePipeline:
    """
    Run RESTler in multiple rounds and feed extracted constraints back into
    the dictionary used by the next round.
    """

    def __init__(
        self,
        rounds: int = 3,
        project_root: str = ".",
        grammar_file: str = "grammar.py",
        initial_dict: str = "dict.json",
        restler_mode: str = "test",
        no_ssl: bool = True,
        fuzz_time_budget: Optional[float] = None,
        search_strategy: Optional[str] = None,
        settings_file: str = "engine_settings.json",
        host: Optional[str] = None,
        target_ip: Optional[str] = None,
        target_port: Optional[int] = None,
        constraint_memory_path: Optional[str] = None,
        reset_constraint_memory: bool = False,
        belief_threshold_high: float = 0.75,
        belief_threshold_low: float = 0.40,
        restler_timeout_sec: Optional[int] = None,
        semantic_timeout_sec: int = 300,
        dict_timeout_sec: int = 180,
        grammar_timeout_sec: int = 180,
        target_reset_cmd: Optional[str] = None,
        target_reset_timeout_sec: int = 300,
        rollback_on_regression: bool = True,
        disable_llm_extraction: bool = False,
        llm_timeout_sec: Optional[float] = None,
        target_coverage_file: Optional[str] = None,
    ):
        self.rounds = rounds
        self.project_root = os.path.abspath(project_root)
        self.base_grammar_file = self._resolve_path(grammar_file)
        self.initial_dict = self._resolve_path(initial_dict)
        self.restler_mode = restler_mode.lower().strip()
        self.no_ssl = no_ssl
        self.fuzz_time_budget = fuzz_time_budget
        self.search_strategy = search_strategy
        self.settings_file = self._resolve_path(settings_file)
        self.host = host
        self.target_ip = target_ip
        self.target_port = target_port
        self.reset_constraint_memory = reset_constraint_memory
        self.belief_threshold_high = belief_threshold_high
        self.belief_threshold_low = belief_threshold_low
        self.restler_timeout_sec = restler_timeout_sec
        self.semantic_timeout_sec = semantic_timeout_sec
        self.dict_timeout_sec = dict_timeout_sec
        self.grammar_timeout_sec = grammar_timeout_sec
        self.target_reset_cmd = target_reset_cmd
        self.target_reset_timeout_sec = target_reset_timeout_sec
        self.rollback_on_regression = rollback_on_regression
        self.disable_llm_extraction = disable_llm_extraction
        self.llm_timeout_sec = llm_timeout_sec
        self.target_coverage_file = self._resolve_optional_path(target_coverage_file)
        self.knowledge_manager = KnowledgeManager(
            belief_threshold_high=belief_threshold_high,
            belief_threshold_low=belief_threshold_low,
        )

        if self.restler_mode not in SUPPORTED_MODES:
            supported = ", ".join(sorted(SUPPORTED_MODES))
            raise ValueError(f"Unsupported restler_mode: {self.restler_mode}. Supported: {supported}")
        if not os.path.exists(self.settings_file):
            raise FileNotFoundError(f"RESTler settings file not found: {self.settings_file}")

        self.dict_dir = os.path.join(self.project_root, "dict")
        self.grammar_dir = os.path.join(self.project_root, "grammar")
        self.rounds_dir = os.path.join(self.project_root, "rounds")
        self.restler_output_dir = os.path.join(self.project_root, self._restler_output_folder())
        self.global_error_logs_dir = os.path.join(self.restler_output_dir, "error_logs")
        self.pipeline_state_path = os.path.join(self.rounds_dir, "pipeline_state.json")
        self.last_restler_result_dir: Optional[str] = None
        self.constraint_memory_path = (
            self._resolve_optional_path(constraint_memory_path)
            if constraint_memory_path
            else os.path.join(self.project_root, "constraint_memory.jsonl")
        )

        os.makedirs(self.dict_dir, exist_ok=True)
        os.makedirs(self.grammar_dir, exist_ok=True)
        os.makedirs(self.rounds_dir, exist_ok=True)

        self.dict_round0 = os.path.join(self.dict_dir, "dict_round0.json")
        self._refresh_round0_dict()
        self.grammar_round0 = os.path.join(self.grammar_dir, "grammar_round0.py")
        self._run_grammar_updater(
            base_grammar_path=self.base_grammar_file,
            dict_path=self.dict_round0,
            output_grammar_path=self.grammar_round0,
        )

    def run(self):
        current_dict = self.dict_round0
        current_grammar = self.grammar_round0
        previous_applied_constraints: Optional[str] = None
        previous_success_summary: Optional[Dict[str, Any]] = None
        best_valid_endpoint_count: Optional[int] = None
        best_dict = current_dict
        best_grammar = current_grammar
        best_applied_constraints: Optional[str] = None

        try:
            for round_id in range(self.rounds):
                print(f"\n{'=' * 20} ROUND {round_id} START {'=' * 20}")

                round_dir = os.path.join(self.rounds_dir, f"round{round_id}")
                os.makedirs(round_dir, exist_ok=True)

                round_error_summary = os.path.join(round_dir, "restler_error_summary.jsonl")
                round_constraints = os.path.join(round_dir, "semantic_constraints.jsonl")
                round_applied_constraints = os.path.join(round_dir, "applied_constraints.jsonl")
                round_speccov = os.path.join(round_dir, "speccov.json")
                round_testing_summary = os.path.join(round_dir, "testing_summary.json")
                round_success_summary = os.path.join(round_dir, "success_summary.json")
                round_experiment_metrics = os.path.join(round_dir, "experiment_metrics.json")
                round_stats = os.path.join(round_dir, "stats.json")
                round_adaptive_stats = os.path.join(round_dir, "adaptive_stats.json")
                next_dict = os.path.join(self.dict_dir, f"dict_round{round_id + 1}.json")
                next_grammar = os.path.join(self.grammar_dir, f"grammar_round{round_id + 1}.py")

                self._write_pipeline_state(round_id, "reset_target", "running")
                self._reset_target_if_configured(round_id)
                self._write_pipeline_state(round_id, "run_restler", "running")
                self._clean_global_error_logs()
                self._run_restler(current_dict, current_grammar)
                self._write_pipeline_state(round_id, "archive_restler_outputs", "running")
                self._archive_error_logs(round_error_summary)
                self._archive_speccov(round_speccov)
                self._archive_testing_summary(round_testing_summary)
                success_summary = self._collect_success_summary(round_speccov)
                self._save_json(round_success_summary, success_summary)
                experiment_metrics = self._collect_experiment_metrics(
                    success_summary=success_summary,
                    testing_summary_path=round_testing_summary,
                )
                self._save_json(round_experiment_metrics, experiment_metrics)

                self._write_pipeline_state(round_id, "semantic_extraction", "running")
                self._run_semantic_extractor(
                    input_path=round_error_summary,
                    output_path=round_constraints,
                    round_id=round_id,
                )

                self._write_pipeline_state(round_id, "knowledge_feedback", "running")
                feedback_summary = self._apply_round_feedback(
                    previous_applied_constraints=previous_applied_constraints,
                    current_constraints_path=round_constraints,
                    current_success_summary=success_summary,
                    previous_success_summary=previous_success_summary,
                    current_round=round_id,
                )

                self._write_pipeline_state(round_id, "knowledge_update", "running")
                knowledge_summary = self._update_knowledge_from_round(
                    constraints_path=round_constraints,
                    round_id=round_id,
                    reset_memory=(self.reset_constraint_memory and round_id == 0),
                )

                self._write_pipeline_state(round_id, "dict_update", "running")
                self._run_dict_updater(
                    constraints_path=round_constraints,
                    base_dict_path=current_dict,
                    output_dict_path=next_dict,
                    current_round=round_id,
                    applied_constraints_path=round_applied_constraints,
                )
                self._write_pipeline_state(round_id, "grammar_update", "running")
                self._run_grammar_updater(
                    base_grammar_path=self.base_grammar_file,
                    dict_path=next_dict,
                    output_grammar_path=next_grammar,
                )

                self._write_pipeline_state(round_id, "collect_stats", "running")
                adaptive_stats = self._collect_adaptive_stats(
                    round_id=round_id,
                    constraints_path=round_constraints,
                    applied_constraints_path=round_applied_constraints,
                    feedback_summary=feedback_summary,
                    knowledge_summary=knowledge_summary,
                    success_summary=success_summary,
                )
                self._save_json(round_adaptive_stats, adaptive_stats)

                stats = self._collect_round_stats(
                    round_id=round_id,
                    dict_path=current_dict,
                    grammar_path=current_grammar,
                    error_summary_path=round_error_summary,
                    constraints_path=round_constraints,
                    applied_constraints_path=round_applied_constraints,
                    speccov_path=round_speccov,
                    success_summary=success_summary,
                    adaptive_stats=adaptive_stats,
                    testing_summary_path=round_testing_summary,
                    experiment_metrics=experiment_metrics,
                )
                next_state = self._select_next_round_state(
                    success_summary=success_summary,
                    next_dict=next_dict,
                    next_grammar=next_grammar,
                    round_applied_constraints=round_applied_constraints,
                    best_valid_endpoint_count=best_valid_endpoint_count,
                    best_dict=best_dict,
                    best_grammar=best_grammar,
                    best_applied_constraints=best_applied_constraints,
                )
                stats["next_round_state"] = next_state["summary"]
                self._save_json(round_stats, stats)

                print(f"[Pipeline] round {round_id} stats saved to: {round_stats}")
                print(f"[Pipeline] adaptive stats saved to: {round_adaptive_stats}")
                print(f"[Pipeline] next dict: {next_dict}")
                print(f"[Pipeline] next grammar: {next_grammar}")

                current_dict = next_state["current_dict"]
                current_grammar = next_state["current_grammar"]
                previous_applied_constraints = next_state["previous_applied_constraints"]
                best_valid_endpoint_count = next_state["best_valid_endpoint_count"]
                best_dict = next_state["best_dict"]
                best_grammar = next_state["best_grammar"]
                best_applied_constraints = next_state["best_applied_constraints"]
                previous_success_summary = success_summary
                self._write_pipeline_state(round_id, "round_complete", "completed")

            self._write_pipeline_state(self.rounds - 1, "all_rounds_finished", "completed")
            print(f"\n{'=' * 20} ALL ROUNDS FINISHED {'=' * 20}")
        except Exception as exc:
            self._write_pipeline_state(
                locals().get("round_id"),
                locals().get("step_name", "pipeline"),
                "failed",
                error=str(exc),
            )
            raise

    def _run_restler(self, dict_path: str, grammar_path: str):
        cmd = self._build_restler_command(dict_path, grammar_path)

        if self.restler_mode in {"fuzz", "fuzz-lean"} and self.fuzz_time_budget is None:
            print(f"[Pipeline] warning: {self.restler_mode} mode is running without --time_budget; it may continue until stopped.")

        self._run_restler_cmd(
            cmd,
            step_name=f"RESTler {self.restler_mode.upper()}",
            timeout_sec=self._resolve_restler_timeout_sec(),
        )

    def _build_restler_command(self, dict_path: str, grammar_path: str) -> List[str]:
        restler_exe = os.path.join(self.project_root, "restler", "Restler.exe")
        cmd = [
            restler_exe,
            self.restler_mode,
            "--grammar_file",
            grammar_path,
            "--dictionary_file",
            dict_path,
        ]

        if self.no_ssl:
            cmd.append("--no_ssl")

        cmd.extend(["--settings", self.settings_file])

        if self.host:
            cmd.extend(["--host", self.host])

        if self.target_ip:
            cmd.extend(["--target_ip", self.target_ip])

        if self.target_port is not None:
            cmd.extend(["--target_port", str(self.target_port)])

        if self.restler_mode in {"fuzz", "fuzz-lean"}:
            if self.fuzz_time_budget is not None:
                cmd.extend(["--time_budget", str(self.fuzz_time_budget)])
            if self.restler_mode == "fuzz" and self.search_strategy:
                cmd.extend(["--search_strategy", self.search_strategy])

        return cmd

    def _run_semantic_extractor(self, input_path: str, output_path: str, round_id: Optional[int] = None):
        cmd = [
            "python",
            "semantic_extractor.py",
            "--input",
            input_path,
            "--output",
            output_path,
        ]
        if round_id is not None:
            cmd.extend(["--round_id", str(round_id)])
        if self.disable_llm_extraction:
            cmd.append("--disable_llm")
        if self.llm_timeout_sec is not None:
            cmd.extend(["--llm_timeout_sec", str(self.llm_timeout_sec)])
        self._run_cmd(cmd, step_name="Semantic Extraction", timeout_sec=self.semantic_timeout_sec)

    def _run_dict_updater(
        self,
        constraints_path: str,
        base_dict_path: str,
        output_dict_path: str,
        current_round: Optional[int] = None,
        applied_constraints_path: Optional[str] = None,
    ):
        cmd = [
            "python",
            "dict_updater.py",
            "--constraints",
            constraints_path,
            "--base_dict",
            base_dict_path,
            "--output_dict",
            output_dict_path,
            "--memory",
            self.constraint_memory_path,
            "--belief_threshold_high",
            str(self.belief_threshold_high),
            "--belief_threshold_low",
            str(self.belief_threshold_low),
        ]
        if current_round is not None:
            cmd.extend(["--current_round", str(current_round)])
        if applied_constraints_path:
            cmd.extend(["--applied_constraints", applied_constraints_path])
        self._run_cmd(cmd, step_name="Dictionary Update", timeout_sec=self.dict_timeout_sec)

    def _run_grammar_updater(self, base_grammar_path: str, dict_path: str, output_grammar_path: str):
        cmd = [
            "python",
            "grammar_updater.py",
            "--base_grammar",
            base_grammar_path,
            "--dict_file",
            dict_path,
            "--output_grammar",
            output_grammar_path,
        ]
        self._run_cmd(cmd, step_name="Grammar Update", timeout_sec=self.grammar_timeout_sec)

    def _reset_target_if_configured(self, round_id: int) -> None:
        if not self.target_reset_cmd:
            return
        self._run_shell_cmd(
            self.target_reset_cmd,
            step_name=f"Target Reset Before Round {round_id}",
            timeout_sec=self.target_reset_timeout_sec,
        )

    def _resolve_restler_timeout_sec(self) -> int:
        if self.restler_timeout_sec is not None:
            return int(self.restler_timeout_sec)
        if self.restler_mode == "test":
            return 1200
        if self.restler_mode in {"fuzz", "fuzz-lean"} and self.fuzz_time_budget is not None:
            return int(self.fuzz_time_budget * 3600) + 300
        if self.restler_mode == "fuzz-lean":
            return 1800
        return 3600

    def _restler_output_folder(self) -> str:
        if self.restler_mode == "fuzz-lean":
            return "FuzzLean"
        if self.restler_mode == "fuzz":
            return "Fuzz"
        return "Test"

    def _update_knowledge_from_round(
        self,
        constraints_path: str,
        round_id: int,
        reset_memory: bool = False,
    ) -> Dict[str, Any]:
        return self.knowledge_manager.update_from_constraints(
            memory_path=self.constraint_memory_path,
            constraints_path=constraints_path,
            round_id=round_id,
            reset_memory=reset_memory,
        )

    def _apply_round_feedback(
        self,
        previous_applied_constraints: Optional[str],
        current_constraints_path: str,
        current_success_summary: Optional[Dict[str, Any]],
        previous_success_summary: Optional[Dict[str, Any]],
        current_round: int,
    ) -> Dict[str, Any]:
        return self.knowledge_manager.apply_feedback(
            memory_path=self.constraint_memory_path,
            applied_constraints_path=previous_applied_constraints,
            current_constraints_path=current_constraints_path,
            current_success_summary=current_success_summary,
            previous_success_summary=previous_success_summary,
            feedback_round=current_round,
        )

    def _clean_global_error_logs(self):
        if not os.path.exists(self.global_error_logs_dir):
            return

        for filename in ["restler_error_summary.jsonl", "semantic_constraints.jsonl"]:
            path = os.path.join(self.global_error_logs_dir, filename)
            if os.path.exists(path):
                os.remove(path)

    def _archive_error_logs(self, target_path: str):
        source_path = self._find_error_summary_file()
        if not source_path:
            raise FileNotFoundError(
                "[Pipeline] expected error log not found. "
                "Checked RESTler output directories but could not find restler_error_summary.jsonl."
            )

        shutil.copy2(source_path, target_path)
        print(f"[Pipeline] archived error log to: {target_path}")

    def _archive_speccov(self, target_path: str):
        source_path = self._find_speccov_file()
        if not source_path:
            print("[Pipeline] warning: speccov.json not found for this round.")
            return

        shutil.copy2(source_path, target_path)
        print(f"[Pipeline] archived speccov to: {target_path}")

    def _archive_testing_summary(self, target_path: str):
        source_path = self._find_testing_summary_file()
        if not source_path:
            print("[Pipeline] warning: testing_summary.json not found for this round.")
            return

        shutil.copy2(source_path, target_path)
        print(f"[Pipeline] archived testing summary to: {target_path}")

    def _find_error_summary_file(self) -> Optional[str]:
        candidate_paths = [
            os.path.join(self.global_error_logs_dir, "restler_error_summary.jsonl"),
            os.path.join(self.project_root, "error_logs", "restler_error_summary.jsonl"),
        ]

        for path in candidate_paths:
            if os.path.exists(path):
                return path

        matches = []
        for root, _, files in os.walk(self.project_root):
            if "restler_error_summary.jsonl" in files:
                matches.append(os.path.join(root, "restler_error_summary.jsonl"))

        if not matches:
            return None

        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    def _find_speccov_file(self) -> Optional[str]:
        if self.last_restler_result_dir:
            candidate = os.path.join(self.last_restler_result_dir, "logs", "speccov.json")
            if os.path.exists(candidate):
                return candidate

        matches = []
        for root, _, files in os.walk(self.restler_output_dir):
            if "speccov.json" in files:
                matches.append(os.path.join(root, "speccov.json"))

        if not matches:
            for root, _, files in os.walk(self.project_root):
                if "speccov.json" in files:
                    matches.append(os.path.join(root, "speccov.json"))

        if not matches:
            return None

        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    def _find_testing_summary_file(self) -> Optional[str]:
        if self.last_restler_result_dir:
            candidate = os.path.join(self.last_restler_result_dir, "logs", "testing_summary.json")
            if os.path.exists(candidate):
                return candidate

        matches = []
        for root, _, files in os.walk(self.restler_output_dir):
            if "testing_summary.json" in files:
                matches.append(os.path.join(root, "testing_summary.json"))

        if not matches:
            for root, _, files in os.walk(self.project_root):
                if "testing_summary.json" in files:
                    matches.append(os.path.join(root, "testing_summary.json"))

        if not matches:
            return None

        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    def _collect_round_stats(
        self,
        round_id: int,
        dict_path: str,
        grammar_path: str,
        error_summary_path: str,
        constraints_path: str,
        applied_constraints_path: str,
        speccov_path: str,
        success_summary: Dict[str, Any],
        adaptive_stats: Dict[str, Any],
        testing_summary_path: str,
        experiment_metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "round_id": round_id,
            "restler_mode": self.restler_mode,
            "dict_path": dict_path,
            "grammar_path": grammar_path,
            "constraint_memory_path": self.constraint_memory_path,
            "testing_summary_path": testing_summary_path,
            "dict_value_counts": self._count_dict_values(dict_path),
            "error_summary_count": self._count_jsonl_lines(error_summary_path),
            "constraint_count": self._count_jsonl_lines(constraints_path),
            "applied_constraint_count": self._count_jsonl_lines(applied_constraints_path),
            "speccov_path": speccov_path,
            "endpoint_total": success_summary.get("endpoint_total", 0),
            "valid_endpoint_count": success_summary.get("valid_endpoint_count", 0),
            "invalid_endpoint_count": success_summary.get("invalid_endpoint_count", 0),
            "status_distribution": self._count_status_distribution(error_summary_path),
            "constraint_type_distribution": self._count_constraint_type_distribution(constraints_path),
            "experiment_metrics": experiment_metrics,
            "adaptive_summary": adaptive_stats,
        }

    def _collect_adaptive_stats(
        self,
        round_id: int,
        constraints_path: str,
        applied_constraints_path: str,
        feedback_summary: Dict[str, Any],
        knowledge_summary: Dict[str, Any],
        success_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        applied_items = self._read_jsonl(applied_constraints_path)
        applied_policy_distribution: Dict[str, int] = {}
        for item in applied_items:
            policy = str(item.get("policy", "unknown"))
            applied_policy_distribution[policy] = applied_policy_distribution.get(policy, 0) + 1

        return {
            "round_id": round_id,
            "constraint_count": self._count_jsonl_lines(constraints_path),
            "applied_constraint_count": len(applied_items),
            "applied_policy_distribution": applied_policy_distribution,
            "success_summary": {
                "endpoint_total": success_summary.get("endpoint_total", 0),
                "valid_endpoint_count": success_summary.get("valid_endpoint_count", 0),
                "invalid_endpoint_count": success_summary.get("invalid_endpoint_count", 0),
            },
            "feedback_summary": feedback_summary,
            "knowledge_summary": knowledge_summary,
        }

    def _count_dict_values(self, dict_path: str) -> Dict[str, int]:
        data = self._load_json(dict_path)
        result = {}
        for key, value in data.items():
            if isinstance(value, list):
                result[key] = len(value)
        return result

    def _count_jsonl_lines(self, path: str) -> int:
        if not os.path.exists(path):
            return 0

        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    def _count_status_distribution(self, error_summary_path: str) -> Dict[str, int]:
        dist = {}
        if not os.path.exists(error_summary_path):
            return dist

        for item in self._read_jsonl(error_summary_path):
            status = str(item.get("status", "unknown"))
            dist[status] = dist.get(status, 0) + 1

        return dist

    def _count_constraint_type_distribution(self, constraints_path: str) -> Dict[str, int]:
        dist = {}
        if not os.path.exists(constraints_path):
            return dist

        for item in self._read_jsonl(constraints_path):
            ctype = str(item.get("constraint_type", "unknown"))
            dist[ctype] = dist.get(ctype, 0) + 1

        return dist

    def _collect_success_summary(self, speccov_path: str) -> Dict[str, Any]:
        if not speccov_path or not os.path.exists(speccov_path):
            return {
                "endpoint_total": 0,
                "valid_endpoint_count": 0,
                "invalid_endpoint_count": 0,
                "endpoint_results": {},
            }

        raw = self._load_json(speccov_path)
        endpoint_results: Dict[str, Dict[str, Any]] = {}
        for _, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            method = str(entry.get("verb", "")).strip().upper()
            endpoint = str(entry.get("endpoint", "")).strip()
            verb_endpoint = str(entry.get("verb_endpoint") or f"{method} {endpoint}").strip()
            if not verb_endpoint:
                continue

            result = {
                "valid": 1 if self._is_successful_endpoint(entry) else 0,
                "status_code": str(entry.get("status_code", "")),
                "status_text": entry.get("status_text"),
                "error_message": entry.get("error_message"),
                "request_order": entry.get("request_order"),
            }

            existing = endpoint_results.get(verb_endpoint)
            if existing is None:
                endpoint_results[verb_endpoint] = result
                continue

            if int(result.get("valid", 0)) > int(existing.get("valid", 0)):
                endpoint_results[verb_endpoint] = result
                continue

            if int(result.get("valid", 0)) == int(existing.get("valid", 0)):
                current_order = entry.get("request_order")
                existing_order = existing.get("request_order")
                if isinstance(current_order, int) and isinstance(existing_order, int) and current_order > existing_order:
                    endpoint_results[verb_endpoint] = result

        valid_endpoint_count = sum(1 for result in endpoint_results.values() if int(result.get("valid", 0)) == 1)
        endpoint_total = len(endpoint_results)
        return {
            "endpoint_total": endpoint_total,
            "valid_endpoint_count": valid_endpoint_count,
            "invalid_endpoint_count": max(0, endpoint_total - valid_endpoint_count),
            "endpoint_results": endpoint_results,
        }

    def _collect_experiment_metrics(
        self,
        success_summary: Dict[str, Any],
        testing_summary_path: Optional[str],
    ) -> Dict[str, Any]:
        testing_summary: Dict[str, Any] = {}
        if testing_summary_path and os.path.exists(testing_summary_path):
            try:
                testing_summary = self._load_json(testing_summary_path)
            except Exception:
                testing_summary = {}

        strts = int(success_summary.get("valid_endpoint_count", 0) or 0)
        spec_coverage = self._parse_fraction_metric(testing_summary.get("final_spec_coverage"))
        rendered_valid = self._parse_fraction_metric(testing_summary.get("rendered_requests_valid_status"))

        return {
            "STRTs": {
                "value": strts,
                "description": "Successfully tested unique request types (approximated by valid_endpoint_count).",
                "source": "success_summary.json",
            },
            "SpecCoverage": {
                **spec_coverage,
                "source": "testing_summary.json",
            },
            "RenderedValidStatus": {
                **rendered_valid,
                "source": "testing_summary.json",
            },
            "PRTT": self._collect_prtt_metric(),
            "Bugs": self._collect_bug_metric(),
            "LOCs": self._collect_locs_metric(),
        }

    def _parse_fraction_metric(self, raw_value: Any) -> Dict[str, Any]:
        raw_text = "" if raw_value is None else str(raw_value).strip()
        match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", raw_text)
        if not match:
            return {
                "raw": raw_value,
                "numerator": None,
                "denominator": None,
                "ratio": None,
                "percent": None,
            }

        numerator = int(match.group(1))
        denominator = int(match.group(2))
        ratio = (numerator / denominator) if denominator else None
        return {
            "raw": raw_text,
            "numerator": numerator,
            "denominator": denominator,
            "ratio": ratio,
            "percent": (ratio * 100.0) if ratio is not None else None,
        }

    def _collect_prtt_metric(self) -> Dict[str, Any]:
        network_logs = self._find_network_log_files()
        status_distribution: Dict[str, int] = {}
        total_responses = 0
        passed_responses = 0
        pattern = re.compile(r"Received:\s*'HTTP/\d(?:\.\d)?\s+(\d{3})\b")

        for path in network_logs:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        match = pattern.search(line)
                        if not match:
                            continue
                        status_code = int(match.group(1))
                        status_key = str(status_code)
                        status_distribution[status_key] = status_distribution.get(status_key, 0) + 1
                        total_responses += 1
                        if 200 <= status_code < 300 or 500 <= status_code < 600:
                            passed_responses += 1
            except Exception:
                continue

        ratio = (passed_responses / total_responses) if total_responses else None
        return {
            "value": ratio,
            "percent": (ratio * 100.0) if ratio is not None else None,
            "passed_responses": passed_responses,
            "total_responses": total_responses,
            "formula": "(2xx + 5xx) / all responses",
            "status_distribution": status_distribution,
            "source_files": network_logs,
        }

    def _collect_bug_metric(self) -> Dict[str, Any]:
        bug_file = self._find_bugs_file()
        if not bug_file:
            return {
                "available": False,
                "total_bug_buckets": 0,
                "reproducible_bug_buckets": 0,
                "server_error_bug_buckets": 0,
                "server_error_reproducible_bug_buckets": 0,
                "unique_signatures": 0,
                "source_file": None,
                "note": "No Bugs.json found for this run.",
            }

        try:
            bug_data = self._load_json(bug_file)
        except Exception as exc:
            return {
                "available": False,
                "total_bug_buckets": 0,
                "reproducible_bug_buckets": 0,
                "server_error_bug_buckets": 0,
                "server_error_reproducible_bug_buckets": 0,
                "unique_signatures": 0,
                "source_file": bug_file,
                "note": f"Failed to parse Bugs.json: {exc}",
            }

        bugs = bug_data.get("bugs", []) if isinstance(bug_data, dict) else []
        total_bug_buckets = 0
        reproducible_bug_buckets = 0
        server_error_bug_buckets = 0
        server_error_reproducible_bug_buckets = 0
        signatures = set()

        for bug in bugs:
            if not isinstance(bug, dict):
                continue
            total_bug_buckets += 1
            checker_name = str(bug.get("checker_name", "unknown"))
            error_code = str(bug.get("error_code", "unknown"))
            filepath = str(bug.get("filepath", "unknown"))
            signatures.add((checker_name, error_code, filepath))

            reproducible = bool(bug.get("reproducible"))
            if reproducible:
                reproducible_bug_buckets += 1

            if error_code.startswith("5"):
                server_error_bug_buckets += 1
                if reproducible:
                    server_error_reproducible_bug_buckets += 1

        return {
            "available": True,
            "total_bug_buckets": total_bug_buckets,
            "reproducible_bug_buckets": reproducible_bug_buckets,
            "server_error_bug_buckets": server_error_bug_buckets,
            "server_error_reproducible_bug_buckets": server_error_reproducible_bug_buckets,
            "unique_signatures": len(signatures),
            "source_file": bug_file,
            "note": "Paper-level unique bugs may still require manual merging by 50X response body and server logs.",
        }

    def _collect_locs_metric(self) -> Dict[str, Any]:
        if not self.target_coverage_file:
            return {
                "available": False,
                "value": None,
                "source_file": None,
                "extraction_mode": None,
                "note": "No target coverage file configured. Use --target_coverage_file to enable LOC collection.",
            }

        if not os.path.exists(self.target_coverage_file):
            return {
                "available": False,
                "value": None,
                "source_file": self.target_coverage_file,
                "extraction_mode": None,
                "note": "Configured target coverage file does not exist.",
            }

        value, mode = self._extract_locs_from_file(self.target_coverage_file)
        return {
            "available": value is not None,
            "value": value,
            "source_file": self.target_coverage_file,
            "extraction_mode": mode,
            "note": None if value is not None else "Could not infer unique covered LOCs from the configured coverage file.",
        }

    def _extract_locs_from_file(self, path: str) -> tuple[Optional[int], Optional[str]]:
        lowered = path.lower()
        if lowered.endswith(".json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return None, None

            direct_value = self._extract_locs_from_json(data)
            if direct_value is not None:
                return direct_value, "json"

            return None, "json"

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [line.strip() for line in f if line.strip()]
        except Exception:
            return None, None

        integer_lines = [int(line) for line in lines if re.fullmatch(r"\d+", line)]
        if integer_lines:
            return len(set(integer_lines)), "line_list"

        for line in lines:
            match = re.search(r"(unique[_\\s-]*code[_\\s-]*lines|unique[_\\s-]*lines|covered[_\\s-]*lines|locs?)\\D+(\\d+)", line, flags=re.I)
            if match:
                return int(match.group(2)), "text_summary"

        return None, None

    def _extract_locs_from_json(self, data: Any) -> Optional[int]:
        if isinstance(data, bool):
            return None
        if isinstance(data, int):
            return data
        if isinstance(data, float):
            return int(data)

        if isinstance(data, dict):
            priority_keys = [
                "unique_code_lines",
                "unique_lines",
                "covered_lines",
                "covered_line_count",
                "line_count",
                "locs",
                "loc",
            ]
            lowered_map = {str(key).lower(): value for key, value in data.items()}
            for key in priority_keys:
                value = lowered_map.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)

            for key in ["lines", "covered", "covered_lines", "executed_lines", "line_hits"]:
                value = lowered_map.get(key)
                extracted = self._extract_unique_line_count(value)
                if extracted is not None:
                    return extracted

            for value in data.values():
                extracted = self._extract_locs_from_json(value)
                if extracted is not None:
                    return extracted

        if isinstance(data, list):
            extracted = self._extract_unique_line_count(data)
            if extracted is not None:
                return extracted
            for item in data:
                nested = self._extract_locs_from_json(item)
                if nested is not None:
                    return nested

        return None

    def _extract_unique_line_count(self, value: Any) -> Optional[int]:
        if isinstance(value, list):
            ints = [item for item in value if isinstance(item, int) and item >= 0]
            if ints and len(ints) == len(value):
                return len(set(ints))
        if isinstance(value, dict):
            numeric_keys = []
            for key in value.keys():
                key_text = str(key).strip()
                if key_text.isdigit():
                    numeric_keys.append(int(key_text))
            if numeric_keys:
                return len(set(numeric_keys))
        return None

    def _find_bugs_file(self) -> Optional[str]:
        if self.last_restler_result_dir:
            candidate = os.path.join(self.last_restler_result_dir, "bug_buckets", "Bugs.json")
            if os.path.exists(candidate):
                return candidate
            return None

        matches = []
        for root, _, files in os.walk(self.restler_output_dir):
            if "Bugs.json" in files:
                matches.append(os.path.join(root, "Bugs.json"))

        if not matches:
            for root, _, files in os.walk(self.project_root):
                if "Bugs.json" in files:
                    matches.append(os.path.join(root, "Bugs.json"))

        if not matches:
            return None

        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    def _find_network_log_files(self) -> List[str]:
        if self.last_restler_result_dir:
            logs_dir = os.path.join(self.last_restler_result_dir, "logs")
            if os.path.isdir(logs_dir):
                matches = []
                for name in os.listdir(logs_dir):
                    if name.startswith("network.testing.") and name.endswith(".txt"):
                        matches.append(os.path.join(logs_dir, name))
                if matches:
                    matches.sort()
                    return matches

        matches = []
        for root, _, files in os.walk(self.restler_output_dir):
            for name in files:
                if name.startswith("network.testing.") and name.endswith(".txt"):
                    matches.append(os.path.join(root, name))

        if not matches:
            for root, _, files in os.walk(self.project_root):
                for name in files:
                    if name.startswith("network.testing.") and name.endswith(".txt"):
                        matches.append(os.path.join(root, name))

        matches.sort(key=os.path.getmtime, reverse=True)
        return matches

    def _is_successful_endpoint(self, entry: Dict[str, Any]) -> bool:
        raw_valid = entry.get("valid", 0)
        if isinstance(raw_valid, bool):
            return raw_valid
        if isinstance(raw_valid, (int, float)):
            return int(raw_valid) == 1
        return str(raw_valid).strip().lower() in {"1", "true", "yes"}

    def _select_next_round_state(
        self,
        success_summary: Dict[str, Any],
        next_dict: str,
        next_grammar: str,
        round_applied_constraints: str,
        best_valid_endpoint_count: Optional[int],
        best_dict: str,
        best_grammar: str,
        best_applied_constraints: Optional[str],
    ) -> Dict[str, Any]:
        current_valid = int(success_summary.get("valid_endpoint_count", 0) or 0)
        if best_valid_endpoint_count is None or current_valid >= best_valid_endpoint_count:
            return {
                "current_dict": next_dict,
                "current_grammar": next_grammar,
                "previous_applied_constraints": round_applied_constraints,
                "best_valid_endpoint_count": current_valid,
                "best_dict": next_dict,
                "best_grammar": next_grammar,
                "best_applied_constraints": round_applied_constraints,
                "summary": {
                    "accepted": True,
                    "rollback": False,
                    "current_valid_endpoint_count": current_valid,
                    "best_valid_endpoint_count": current_valid,
                    "next_dict": next_dict,
                    "next_grammar": next_grammar,
                },
            }

        if not self.rollback_on_regression:
            return {
                "current_dict": next_dict,
                "current_grammar": next_grammar,
                "previous_applied_constraints": round_applied_constraints,
                "best_valid_endpoint_count": best_valid_endpoint_count,
                "best_dict": best_dict,
                "best_grammar": best_grammar,
                "best_applied_constraints": best_applied_constraints,
                "summary": {
                    "accepted": True,
                    "rollback": False,
                    "regression_allowed": True,
                    "current_valid_endpoint_count": current_valid,
                    "best_valid_endpoint_count": best_valid_endpoint_count,
                    "next_dict": next_dict,
                    "next_grammar": next_grammar,
                },
            }

        print(
            "[Pipeline] success regression detected: "
            f"{current_valid} valid endpoints < best {best_valid_endpoint_count}. "
            "Next round will reuse the best known dict/grammar."
        )
        return {
            "current_dict": best_dict,
            "current_grammar": best_grammar,
            "previous_applied_constraints": best_applied_constraints,
            "best_valid_endpoint_count": best_valid_endpoint_count,
            "best_dict": best_dict,
            "best_grammar": best_grammar,
            "best_applied_constraints": best_applied_constraints,
            "summary": {
                "accepted": False,
                "rollback": True,
                "current_valid_endpoint_count": current_valid,
                "best_valid_endpoint_count": best_valid_endpoint_count,
                "next_dict": best_dict,
                "next_grammar": best_grammar,
                "rejected_dict": next_dict,
                "rejected_grammar": next_grammar,
            },
        }

    def _run_restler_cmd(self, cmd: List[str], step_name: str, timeout_sec: Optional[int] = None):
        print(f"\n[Pipeline] ===== {step_name} =====")
        print(f"[Pipeline] CMD: {' '.join(self._quote_arg(arg) for arg in cmd)}")

        started_at = time.time()
        known_result_dirs = self._list_restler_result_dirs()
        process = subprocess.Popen(cmd, cwd=self.project_root)
        completion_seen_at: Optional[float] = None
        current_result_dir: Optional[str] = None

        while True:
            returncode = process.poll()
            if returncode is not None:
                if returncode != 0:
                    raise RuntimeError(f"[Pipeline] {step_name} failed with return code {returncode}")
                self.last_restler_result_dir = current_result_dir or self._find_current_restler_result_dir(known_result_dirs, started_at)
                return

            current_result_dir = self._find_current_restler_result_dir(known_result_dirs, started_at)
            if self._restler_outputs_completed(current_result_dir, started_at):
                if completion_seen_at is None:
                    completion_seen_at = time.time()
                elif time.time() - completion_seen_at >= 5:
                    print(
                        "[Pipeline] RESTler final summary is complete, "
                        "but the process is still running. Terminating it and continuing."
                    )
                    self._terminate_process(process, step_name)
                    self.last_restler_result_dir = current_result_dir
                    return
            else:
                completion_seen_at = None

            if timeout_sec is not None and time.time() - started_at > timeout_sec:
                self.last_restler_result_dir = current_result_dir
                self._terminate_process(process, step_name)
                raise RuntimeError(f"[Pipeline] {step_name} timed out after {timeout_sec} seconds")

            time.sleep(2)

    def _list_restler_result_dirs(self) -> set:
        results_root = os.path.join(self.restler_output_dir, "RestlerResults")
        if not os.path.isdir(results_root):
            return set()
        return {
            os.path.join(results_root, name)
            for name in os.listdir(results_root)
            if os.path.isdir(os.path.join(results_root, name))
        }

    def _find_current_restler_result_dir(self, known_result_dirs: set, started_at: float) -> Optional[str]:
        results_root = os.path.join(self.restler_output_dir, "RestlerResults")
        if not os.path.isdir(results_root):
            return None

        candidates = []
        for name in os.listdir(results_root):
            path = os.path.join(results_root, name)
            if not os.path.isdir(path):
                continue
            if path in known_result_dirs and os.path.getmtime(path) < started_at:
                continue
            candidates.append(path)

        if not candidates:
            return None

        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]

    def _restler_outputs_completed(self, result_dir: Optional[str], started_at: float) -> bool:
        if not result_dir:
            return False

        logs_dir = os.path.join(result_dir, "logs")
        testing_summary = os.path.join(logs_dir, "testing_summary.json")
        speccov = os.path.join(logs_dir, "speccov.json")
        main_log = os.path.join(logs_dir, "main.txt")
        if not testing_summary or not speccov:
            return False
        if not os.path.exists(testing_summary) or not os.path.exists(speccov):
            return False

        if os.path.getmtime(testing_summary) < started_at:
            return False
        if os.path.getmtime(speccov) < started_at:
            return False

        try:
            summary = self._load_json(testing_summary)
        except Exception:
            return False

        if bool(summary.get("final_spec_coverage") or summary.get("rendered_requests_valid_status")):
            return True

        if os.path.exists(main_log) and os.path.getmtime(main_log) >= started_at:
            try:
                with open(main_log, "r", encoding="utf-8", errors="ignore") as f:
                    tail = f.read()[-4096:]
                return "Testing completed -- below are the final stats:" in tail
            except Exception:
                return False

        return False

    def _terminate_process(self, process: subprocess.Popen, step_name: str) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"[Pipeline] {step_name} did not exit after terminate; killing it.")
            process.kill()
            process.wait(timeout=10)

    def _run_cmd(self, cmd: List[str], step_name: str, timeout_sec: Optional[int] = None):
        print(f"\n[Pipeline] ===== {step_name} =====")
        print(f"[Pipeline] CMD: {' '.join(self._quote_arg(arg) for arg in cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"[Pipeline] {step_name} timed out after {timeout_sec} seconds"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(f"[Pipeline] {step_name} failed with return code {result.returncode}")

    def _run_shell_cmd(self, cmd: str, step_name: str, timeout_sec: Optional[int] = None):
        print(f"\n[Pipeline] ===== {step_name} =====")
        print(f"[Pipeline] CMD: {cmd}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                shell=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"[Pipeline] {step_name} timed out after {timeout_sec} seconds"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(f"[Pipeline] {step_name} failed with return code {result.returncode}")

    def _refresh_round0_dict(self) -> None:
        if os.path.exists(self.dict_round0):
            os.remove(self.dict_round0)
        shutil.copy2(self.initial_dict, self.dict_round0)
        print(f"[Pipeline] refreshed round0 dict from initial dict: {self.dict_round0}")

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.project_root, path)

    def _resolve_optional_path(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        return self._resolve_path(path)

    def _quote_arg(self, arg: str) -> str:
        if not arg or any(ch.isspace() for ch in arg):
            return f'"{arg}"'
        return arg

    def _load_json(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, path: str, data: Dict[str, Any]):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _write_pipeline_state(
        self,
        round_id: Optional[int],
        step: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        state = {
            "round_id": round_id,
            "step": step,
            "status": status,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": error,
        }
        self._save_json(self.pipeline_state_path, state)

    def _read_jsonl(self, path: str) -> List[Dict[str, Any]]:
        results = []
        if not os.path.exists(path):
            return results

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except Exception:
                    continue
        return results


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run iterative RESTler rounds in test, fuzz, or fuzz-lean mode.")
    parser.add_argument("--rounds", type=int, default=3, help="Number of iterative rounds to run.")
    parser.add_argument("--project_root", default=".", help="RESTler working directory.")
    parser.add_argument("--grammar_file", default="grammar.py", help="RESTler grammar file.")
    parser.add_argument("--initial_dict", default="dict.json", help="Initial RESTler dictionary file.")
    parser.add_argument(
        "--mode",
        "--restler_mode",
        dest="restler_mode",
        choices=sorted(SUPPORTED_MODES),
        default="test",
        help="RESTler mode to use.",
    )
    parser.add_argument("--no_ssl", action="store_true", default=True, help="Disable SSL when talking to the target service.")
    parser.add_argument("--ssl", dest="no_ssl", action="store_false", help="Enable SSL when talking to the target service.")
    parser.add_argument("--time_budget", type=float, default=None, help="RESTler fuzz time budget in hours. Used in fuzz and fuzz-lean modes.")
    parser.add_argument(
        "--search_strategy",
        choices=["bfs-fast", "bfs", "bfs-cheap", "random-walk"],
        default=None,
        help="RESTler fuzz search strategy. Only used in fuzz mode.",
    )
    parser.add_argument("--settings", dest="settings_file", default="engine_settings.json", help="RESTler engine settings file. Defaults to engine_settings.json.")
    parser.add_argument("--host", default=None, help="Override the Host header.")
    parser.add_argument("--target_ip", default=None, help="Override the target IP.")
    parser.add_argument("--target_port", type=int, default=None, help="Override the target port.")
    parser.add_argument(
        "--constraint_memory",
        "--knowledge_memory",
        dest="constraint_memory_path",
        default=None,
        help="Path to persistent adaptive knowledge memory.",
    )
    parser.add_argument("--reset_constraint_memory", action="store_true", help="Reset the persistent constraint memory before round 0.")
    parser.add_argument("--belief_threshold_high", type=float, default=0.75, help="Belief score threshold for exploit policy.")
    parser.add_argument("--belief_threshold_low", type=float, default=0.40, help="Belief score threshold for balance policy.")
    parser.add_argument("--restler_timeout_sec", type=int, default=None, help="Per-round RESTler timeout in seconds.")
    parser.add_argument("--semantic_timeout_sec", type=int, default=300, help="Semantic extraction timeout in seconds.")
    parser.add_argument("--dict_timeout_sec", type=int, default=180, help="Dictionary update timeout in seconds.")
    parser.add_argument("--grammar_timeout_sec", type=int, default=180, help="Grammar update timeout in seconds.")
    parser.add_argument(
        "--disable_llm_extraction",
        action="store_true",
        help="Disable LLM-based semantic extraction and use rule-based extraction only.",
    )
    parser.add_argument(
        "--llm_timeout_sec",
        type=float,
        default=None,
        help="Per-request LLM timeout passed to semantic_extractor.py.",
    )
    parser.add_argument(
        "--target_reset_cmd",
        default=None,
        help="Optional shell command used to reset the target service before every RESTler round.",
    )
    parser.add_argument(
        "--target_reset_timeout_sec",
        type=int,
        default=300,
        help="Timeout for --target_reset_cmd in seconds.",
    )
    parser.add_argument(
        "--disable_regression_rollback",
        action="store_true",
        help="Disable automatic rollback to the best dict/grammar when valid endpoint count decreases.",
    )
    parser.add_argument(
        "--target_coverage_file",
        default=None,
        help="Optional target-side coverage summary file used to collect LOCs for each run.",
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    pipeline = IterativePipeline(
        rounds=args.rounds,
        project_root=args.project_root,
        grammar_file=args.grammar_file,
        initial_dict=args.initial_dict,
        restler_mode=args.restler_mode,
        no_ssl=args.no_ssl,
        fuzz_time_budget=args.time_budget,
        search_strategy=args.search_strategy,
        settings_file=args.settings_file,
        host=args.host,
        target_ip=args.target_ip,
        target_port=args.target_port,
        constraint_memory_path=args.constraint_memory_path,
        reset_constraint_memory=args.reset_constraint_memory,
        belief_threshold_high=args.belief_threshold_high,
        belief_threshold_low=args.belief_threshold_low,
        restler_timeout_sec=args.restler_timeout_sec,
        semantic_timeout_sec=args.semantic_timeout_sec,
        dict_timeout_sec=args.dict_timeout_sec,
        grammar_timeout_sec=args.grammar_timeout_sec,
        target_reset_cmd=args.target_reset_cmd,
        target_reset_timeout_sec=args.target_reset_timeout_sec,
        rollback_on_regression=not args.disable_regression_rollback,
        disable_llm_extraction=args.disable_llm_extraction,
        llm_timeout_sec=args.llm_timeout_sec,
        target_coverage_file=args.target_coverage_file,
    )
    pipeline.run()


if __name__ == "__main__":
    main()
