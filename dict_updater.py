# dict_updater.py
# -*- coding: utf-8 -*-

import copy
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from knowledge_manager import KnowledgeManager


class DictUpdater:
    """
    Enhance the RESTler mutations dictionary based on semantic constraints
    extracted from previous execution rounds.
    """

    SUPPORTED_CONSTRAINT_TYPES = {
        "minimum",
        "maximum",
        "enum",
        "format",
        "not_found",
        "required",
        "minLength",
        "maxLength",
        "pattern",
    }

    LIST_DICT_KEYS = [
        "restler_fuzzable_string",
        "restler_fuzzable_string_unquoted",
        "restler_fuzzable_datetime",
        "restler_fuzzable_datetime_unquoted",
        "restler_fuzzable_date",
        "restler_fuzzable_date_unquoted",
        "restler_fuzzable_uuid4",
        "restler_fuzzable_uuid4_unquoted",
        "restler_fuzzable_int",
        "restler_fuzzable_number",
        "restler_fuzzable_bool",
        "restler_fuzzable_object",
    ]

    def __init__(
        self,
        min_confidence: float = 0.80,
        min_count: int = 1,
        enable_not_found: bool = False,
        inject_invalid_values: bool = True,
    ):
        self.min_confidence = min_confidence
        self.min_count = min_count
        self.enable_not_found = enable_not_found
        self.inject_invalid_values = inject_invalid_values

        self.default_dict_schema = {
            "restler_fuzzable_string": [],
            "restler_fuzzable_string_unquoted": [],
            "restler_fuzzable_datetime": [],
            "restler_fuzzable_datetime_unquoted": [],
            "restler_fuzzable_date": [],
            "restler_fuzzable_date_unquoted": [],
            "restler_fuzzable_uuid4": [],
            "restler_fuzzable_uuid4_unquoted": [],
            "restler_fuzzable_int": [],
            "restler_fuzzable_number": [],
            "restler_fuzzable_bool": [],
            "restler_fuzzable_object": [],
            "restler_custom_payload": {},
            "restler_custom_payload_unquoted": {},
            "restler_custom_payload_uuid4_suffix": {},
            "restler_custom_payload_header": {},
            "restler_custom_payload_header_unquoted": {},
            "restler_custom_payload_query": {},
            "restler_custom_payload_query_unquoted": {},
        }

    def run(
        self,
        constraints_path: str,
        base_dict_path: str,
        output_dict_path: str,
        memory_path: Optional[str] = None,
        current_round: Optional[int] = None,
        reset_memory: bool = False,
        applied_constraints_path: Optional[str] = None,
        belief_threshold_high: float = 0.75,
        belief_threshold_low: float = 0.40,
    ) -> None:
        constraints = self._read_jsonl(constraints_path)
        print(f"[DictUpdater] loaded constraints: {len(constraints)}")

        base_dict = self._load_json(base_dict_path)
        enhanced_dict = self._prepare_base_dict(base_dict)

        filtered_constraints = self._filter_constraints(constraints)
        print(f"[DictUpdater] usable constraints: {len(filtered_constraints)}")

        plans = self._aggregate_constraints(filtered_constraints)
        applied_items = self._build_applied_records_from_plans(plans, current_round)
        print(f"[DictUpdater] aggregated current parameter plans: {len(plans)}")

        if memory_path:
            knowledge_items = self._load_knowledge_items(
                memory_path=memory_path,
                current_round=current_round,
                belief_threshold_high=belief_threshold_high,
                belief_threshold_low=belief_threshold_low,
            )
            if knowledge_items:
                plans = self._aggregate_knowledge_items(knowledge_items)
                applied_items = self._build_applied_records_from_knowledge(knowledge_items, current_round)
                print(f"[DictUpdater] loaded adaptive knowledge items: {len(knowledge_items)}")
                print(f"[DictUpdater] aggregated adaptive parameter plans: {len(plans)}")
            elif reset_memory and os.path.exists(memory_path):
                os.remove(memory_path)
                print(f"[DictUpdater] reset constraint memory: {memory_path}")

        for plan in plans:
            self._apply_parameter_plan(enhanced_dict, plan)

        self._deduplicate_all(enhanced_dict)
        self._save_json(output_dict_path, enhanced_dict)
        self._write_applied_constraints(applied_constraints_path, applied_items)

        print(f"[DictUpdater] enhanced dict saved to: {output_dict_path}")

    def _read_jsonl(self, path: str) -> List[Dict[str, Any]]:
        results = []
        if not path or not os.path.exists(path):
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

    def _load_json(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, path: str, data: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _prepare_base_dict(self, base_dict: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(base_dict)

        for key, default_value in self.default_dict_schema.items():
            if key not in result:
                result[key] = copy.deepcopy(default_value)
                continue

            if isinstance(default_value, list) and not isinstance(result[key], list):
                result[key] = copy.deepcopy(default_value)
            elif isinstance(default_value, dict) and not isinstance(result[key], dict):
                result[key] = copy.deepcopy(default_value)

        return result

    def _filter_constraints(self, constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        usable = []

        for constraint in constraints:
            confidence = float(constraint.get("confidence", 0.0))
            count = int(constraint.get("count", 1))
            ctype = constraint.get("constraint_type")
            parameter = constraint.get("parameter")

            if confidence < self.min_confidence:
                continue

            if count < self.min_count:
                continue

            if not parameter or parameter == "unknown":
                continue

            if ctype not in self.SUPPORTED_CONSTRAINT_TYPES:
                continue

            if ctype == "not_found" and not self.enable_not_found:
                continue

            if self._looks_like_route_level_failure(constraint):
                continue

            usable.append(constraint)

        return usable

    def _merge_with_memory(
        self,
        current_plans: List[Dict[str, Any]],
        memory_path: str,
        current_round: Optional[int],
        reset_memory: bool,
    ) -> List[Dict[str, Any]]:
        if reset_memory and os.path.exists(memory_path):
            os.remove(memory_path)
            print(f"[DictUpdater] reset constraint memory: {memory_path}")

        memory_plans = self._read_constraint_memory(memory_path)
        print(f"[DictUpdater] loaded memory plans: {len(memory_plans)}")

        merged: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

        for plan in memory_plans:
            normalized = self._normalize_memory_plan(plan)
            merged[self._plan_key(normalized)] = normalized

        for plan in current_plans:
            normalized = self._normalize_memory_plan(plan)
            key = self._plan_key(normalized)

            if key not in merged:
                merged[key] = self._initialize_memory_metadata(normalized, current_round)
            else:
                merged[key] = self._merge_two_plans(merged[key], normalized, current_round)

        merged_plans = list(merged.values())
        merged_plans.sort(
            key=lambda item: (
                item.get("endpoint", ""),
                item.get("method", ""),
                item.get("location", ""),
                item.get("parameter", ""),
            )
        )

        self._write_constraint_memory(memory_path, merged_plans)
        return merged_plans

    def _read_constraint_memory(self, path: str) -> List[Dict[str, Any]]:
        return self._read_jsonl(path)

    def _write_constraint_memory(self, path: str, plans: List[Dict[str, Any]]) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            for plan in plans:
                f.write(json.dumps(self._serialize_plan(plan), ensure_ascii=False) + "\n")

        print(f"[DictUpdater] constraint memory updated: {path}")

    def _plan_key(self, plan: Dict[str, Any]) -> Tuple[str, str, str, str]:
        return (
            str(plan.get("endpoint", "")),
            str(plan.get("method", "")),
            str(plan.get("parameter", "")),
            str(plan.get("location", "unknown")),
        )

    def _normalize_memory_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._new_plan(
            endpoint=str(plan.get("endpoint", "")),
            method=str(plan.get("method", "")),
            parameter=str(plan.get("parameter", "")),
            location=str(plan.get("location", "unknown")),
        )
        normalized["minimum"] = self._to_number(plan.get("minimum"))
        normalized["maximum"] = self._to_number(plan.get("maximum"))
        normalized["minLength"] = self._to_int(plan.get("minLength"))
        normalized["maxLength"] = self._to_int(plan.get("maxLength"))
        normalized["enum"] = []
        normalized["enum_seen"] = set()

        for item in plan.get("enum", []) or []:
            value = self._normalize_scalar(item)
            if value is None or value in normalized["enum_seen"]:
                continue
            normalized["enum_seen"].add(value)
            normalized["enum"].append(value)

        normalized["format"] = self._normalize_scalar(plan.get("format"))
        normalized["pattern"] = self._normalize_scalar(plan.get("pattern"))
        normalized["required"] = bool(plan.get("required", False))
        normalized["not_found"] = bool(plan.get("not_found", False))
        normalized["total_count"] = int(plan.get("total_count", 0))
        normalized["max_confidence"] = float(plan.get("max_confidence", 0.0))
        normalized["first_seen_round"] = self._to_int(plan.get("first_seen_round"))
        normalized["last_seen_round"] = self._to_int(plan.get("last_seen_round"))
        normalized["update_count"] = int(plan.get("update_count", 0))
        return normalized

    def _initialize_memory_metadata(self, plan: Dict[str, Any], current_round: Optional[int]) -> Dict[str, Any]:
        initialized = dict(plan)
        initialized["first_seen_round"] = current_round
        initialized["last_seen_round"] = current_round
        initialized["update_count"] = 1
        return initialized

    def _merge_two_plans(
        self,
        existing: Dict[str, Any],
        current: Dict[str, Any],
        current_round: Optional[int],
    ) -> Dict[str, Any]:
        merged = dict(existing)
        merged["enum_seen"] = set(existing.get("enum", []))

        if current.get("minimum") is not None:
            merged["minimum"] = current["minimum"] if merged.get("minimum") is None else max(merged["minimum"], current["minimum"])

        if current.get("maximum") is not None:
            merged["maximum"] = current["maximum"] if merged.get("maximum") is None else min(merged["maximum"], current["maximum"])

        if current.get("minLength") is not None:
            merged["minLength"] = current["minLength"] if merged.get("minLength") is None else max(merged["minLength"], current["minLength"])

        if current.get("maxLength") is not None:
            merged["maxLength"] = current["maxLength"] if merged.get("maxLength") is None else min(merged["maxLength"], current["maxLength"])

        for item in current.get("enum", []):
            if item not in merged["enum_seen"]:
                merged["enum_seen"].add(item)
                merged["enum"].append(item)

        if current.get("format"):
            merged["format"] = current["format"]

        if current.get("pattern"):
            merged["pattern"] = current["pattern"]

        merged["required"] = bool(merged.get("required")) or bool(current.get("required"))
        merged["not_found"] = bool(merged.get("not_found")) or bool(current.get("not_found"))
        merged["total_count"] = int(merged.get("total_count", 0)) + int(current.get("total_count", 0))
        merged["max_confidence"] = max(float(merged.get("max_confidence", 0.0)), float(current.get("max_confidence", 0.0)))

        if merged.get("first_seen_round") is None:
            merged["first_seen_round"] = current_round
        merged["last_seen_round"] = current_round if current_round is not None else merged.get("last_seen_round")
        merged["update_count"] = int(merged.get("update_count", 0)) + 1
        return merged

    def _serialize_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        serializable = {}
        for key, value in plan.items():
            if key == "enum_seen":
                continue
            serializable[key] = value
        return serializable

    def _load_knowledge_items(
        self,
        memory_path: str,
        current_round: Optional[int],
        belief_threshold_high: float,
        belief_threshold_low: float,
    ) -> List[Dict[str, Any]]:
        manager = KnowledgeManager(
            belief_threshold_high=belief_threshold_high,
            belief_threshold_low=belief_threshold_low,
        )
        items = manager.load(memory_path)
        usable_items = []
        for item in items:
            item = manager._refresh_item(item, current_round=current_round)
            item["policy"] = manager.choose_policy(item)
            if self._is_knowledge_item_usable(item):
                usable_items.append(item)
        return usable_items

    def _is_knowledge_item_usable(self, item: Dict[str, Any]) -> bool:
        if item.get("constraint_type") not in self.SUPPORTED_CONSTRAINT_TYPES:
            return False
        if not item.get("parameter") or item.get("parameter") == "unknown":
            return False
        if item.get("status") in {"stale", "conflicted", "invalid"}:
            return False
        if self._looks_like_route_level_failure(item):
            return False
        if item.get("constraint_type") == "not_found" and not self.enable_not_found:
            return False
        if item.get("constraint_type") == "required":
            if item.get("location") == "path":
                return False
            if int(item.get("resolved_rounds", 0) or 0) <= 0 and int(item.get("persistent_error_rounds", 0) or 0) > 0:
                return False
        belief = float(item.get("belief_score", 0.0))
        total_observations = int(item.get("total_observations", 0))
        success_rounds = int(item.get("success_rounds", 0) or 0)
        return belief >= 0.15 or total_observations >= 2 or success_rounds > 0

    def _looks_like_route_level_failure(self, item: Dict[str, Any]) -> bool:
        status_code = str(item.get("last_status_code", item.get("status", ""))).strip()
        evidence = self._normalize_text(
            item.get("last_evidence_text")
            or item.get("error_signature")
            or item.get("evidence_text")
            or item.get("evidence")
            or ""
        )
        if status_code != "404":
            return False
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

    def _aggregate_knowledge_items(self, knowledge_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

        for item in knowledge_items:
            key = self._plan_key(item)
            if key not in grouped:
                grouped[key] = self._new_plan(
                    endpoint=key[0],
                    method=key[1],
                    parameter=key[2],
                    location=key[3],
                )
                grouped[key]["belief_score"] = 0.0
                grouped[key]["status"] = "candidate"
                grouped[key]["policy_votes"] = []
                grouped[key]["knowledge_keys"] = []
                grouped[key]["knowledge_count"] = 0

            self._merge_knowledge_item_into_plan(grouped[key], item)

        results = []
        for plan in grouped.values():
            plan.pop("enum_seen", None)
            plan["policy"] = self._resolve_plan_policy(plan)
            plan["belief_score"] = round(float(plan.get("belief_score", 0.0)), 4)
            plan["knowledge_keys"] = self._deduplicate_list(plan.get("knowledge_keys", []))
            results.append(plan)

        results.sort(
            key=lambda item: (
                item.get("endpoint", ""),
                item.get("method", ""),
                item.get("location", ""),
                item.get("parameter", ""),
            )
        )
        return results

    def _merge_knowledge_item_into_plan(self, plan: Dict[str, Any], item: Dict[str, Any]) -> None:
        constraint = {
            "constraint_type": item.get("constraint_type"),
            "constraint_value": item.get("constraint_value"),
            "confidence": item.get("max_confidence", item.get("belief_score", 0.0)),
            "count": item.get("total_observations", 1),
        }
        self._merge_constraint_into_plan(plan, constraint)
        plan["belief_score"] = max(float(plan.get("belief_score", 0.0)), float(item.get("belief_score", 0.0)))
        plan["status"] = self._merge_plan_status(plan.get("status"), item.get("status"))
        plan.setdefault("policy_votes", []).append(item.get("policy", "balance"))
        plan.setdefault("knowledge_keys", []).append(item.get("knowledge_key"))
        plan["knowledge_count"] = int(plan.get("knowledge_count", 0)) + 1

    def _merge_plan_status(self, current_status: Optional[str], incoming_status: Optional[str]) -> str:
        priority = {
            "conflicted": 4,
            "stale": 3,
            "confirmed": 2,
            "candidate": 1,
            "weak": 0,
            None: -1,
        }
        if priority.get(incoming_status, -1) >= priority.get(current_status, -1):
            return incoming_status or "candidate"
        return current_status or "candidate"

    def _resolve_plan_policy(self, plan: Dict[str, Any]) -> str:
        votes = set(plan.get("policy_votes", []))
        if not votes:
            return "balance"
        if votes == {"exploit"}:
            return "exploit"
        if votes == {"explore"}:
            return "explore"
        return "balance"

    def _build_applied_records_from_knowledge(
        self,
        knowledge_items: List[Dict[str, Any]],
        current_round: Optional[int],
    ) -> List[Dict[str, Any]]:
        results = []
        for item in knowledge_items:
            results.append(
                {
                    "round_id": current_round,
                    "knowledge_key": item.get("knowledge_key"),
                    "endpoint": item.get("endpoint"),
                    "method": item.get("method"),
                    "parameter": item.get("parameter"),
                    "location": item.get("location"),
                    "constraint_type": item.get("constraint_type"),
                    "constraint_value": item.get("constraint_value"),
                    "belief_score": round(float(item.get("belief_score", 0.0)), 4),
                    "status": item.get("status"),
                    "policy": item.get("policy"),
                    "total_observations": item.get("total_observations", 0),
                    "success_rounds": item.get("success_rounds", 0),
                }
            )
        return results

    def _build_applied_records_from_plans(
        self,
        plans: List[Dict[str, Any]],
        current_round: Optional[int],
    ) -> List[Dict[str, Any]]:
        results = []
        for plan in plans:
            results.append(
                {
                    "round_id": current_round,
                    "knowledge_key": self._constraint_key_from_parts(
                        endpoint=plan.get("endpoint", ""),
                        method=plan.get("method", ""),
                        parameter=plan.get("parameter", ""),
                        location=plan.get("location", "unknown"),
                        constraint_type="parameter_plan",
                        constraint_value={
                            "minimum": plan.get("minimum"),
                            "maximum": plan.get("maximum"),
                            "minLength": plan.get("minLength"),
                            "maxLength": plan.get("maxLength"),
                            "enum": plan.get("enum"),
                            "format": plan.get("format"),
                            "pattern": plan.get("pattern"),
                            "required": plan.get("required"),
                            "not_found": plan.get("not_found"),
                        },
                    ),
                    "endpoint": plan.get("endpoint"),
                    "method": plan.get("method"),
                    "parameter": plan.get("parameter"),
                    "location": plan.get("location"),
                    "constraint_type": "parameter_plan",
                    "constraint_value": {
                        "minimum": plan.get("minimum"),
                        "maximum": plan.get("maximum"),
                    },
                    "belief_score": round(float(plan.get("max_confidence", 0.0)), 4),
                    "status": plan.get("status", "candidate"),
                    "policy": plan.get("policy", "balance"),
                    "total_observations": plan.get("total_count", 0),
                }
            )
        return results

    def _constraint_key_from_parts(
        self,
        endpoint: str,
        method: str,
        parameter: str,
        location: str,
        constraint_type: str,
        constraint_value: Any,
    ) -> str:
        serialized_value = json.dumps(constraint_value, sort_keys=True, ensure_ascii=False)
        return "||".join(
            [
                str(endpoint or ""),
                str(method or ""),
                str(parameter or ""),
                str(location or "unknown"),
                str(constraint_type or "unknown"),
                serialized_value,
            ]
        )

    def _write_applied_constraints(self, path: Optional[str], items: List[Dict[str, Any]]) -> None:
        if not path:
            return
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"[DictUpdater] applied constraints saved to: {path}")

    def _aggregate_constraints(self, constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

        for constraint in constraints:
            key = self._plan_key(constraint)

            if key not in grouped:
                grouped[key] = self._new_plan(
                    endpoint=key[0],
                    method=key[1],
                    parameter=key[2],
                    location=key[3],
                )

            self._merge_constraint_into_plan(grouped[key], constraint)

        results = []
        for plan in grouped.values():
            plan.pop("enum_seen", None)
            results.append(plan)

        return results

    def _new_plan(self, endpoint: str, method: str, parameter: str, location: str) -> Dict[str, Any]:
        return {
            "endpoint": endpoint,
            "method": method,
            "parameter": parameter,
            "location": location,
            "minimum": None,
            "maximum": None,
            "minLength": None,
            "maxLength": None,
            "enum": [],
            "enum_seen": set(),
            "format": None,
            "pattern": None,
            "required": False,
            "not_found": False,
            "total_count": 0,
            "max_confidence": 0.0,
            "belief_score": 0.0,
            "status": "candidate",
            "policy": "balance",
        }

    def _merge_constraint_into_plan(self, plan: Dict[str, Any], constraint: Dict[str, Any]) -> None:
        ctype = constraint.get("constraint_type")
        value = constraint.get("constraint_value")
        confidence = float(constraint.get("confidence", 0.0))
        count = int(constraint.get("count", 1))

        plan["total_count"] = int(plan.get("total_count", 0)) + count
        plan["max_confidence"] = max(float(plan.get("max_confidence", 0.0)), confidence)

        if ctype == "minimum":
            number = self._to_number(value)
            if number is not None:
                plan["minimum"] = number if plan["minimum"] is None else max(plan["minimum"], number)

        elif ctype == "maximum":
            number = self._to_number(value)
            if number is not None:
                plan["maximum"] = number if plan["maximum"] is None else min(plan["maximum"], number)

        elif ctype == "minLength":
            number = self._to_int(value)
            if number is not None:
                plan["minLength"] = number if plan["minLength"] is None else max(plan["minLength"], number)

        elif ctype == "maxLength":
            number = self._to_int(value)
            if number is not None:
                plan["maxLength"] = number if plan["maxLength"] is None else min(plan["maxLength"], number)

        elif ctype == "enum" and isinstance(value, list):
            enum_seen = plan.setdefault("enum_seen", set())
            for item in value:
                normalized = self._normalize_scalar(item)
                if normalized is None or normalized in enum_seen:
                    continue
                enum_seen.add(normalized)
                plan["enum"].append(normalized)

        elif ctype == "format" and value:
            plan["format"] = str(value).strip().lower()

        elif ctype == "pattern" and value:
            plan["pattern"] = str(value).strip()

        elif ctype == "required":
            plan["required"] = True

        elif ctype == "not_found":
            plan["not_found"] = True

    def _apply_parameter_plan(self, dictionary: Dict[str, Any], plan: Dict[str, Any]) -> None:
        print(
            "[DictUpdater] applying plan: "
            f"parameter={plan['parameter']}, location={plan['location']}, "
            f"min={plan['minimum']}, max={plan['maximum']}, "
            f"minLength={plan['minLength']}, maxLength={plan['maxLength']}, "
            f"enum={len(plan['enum'])}, format={plan['format']}, "
            f"pattern={plan['pattern']}, required={plan['required']}, "
            f"not_found={plan['not_found']}, belief_score={plan.get('belief_score')}, "
            f"status={plan.get('status')}, policy={plan.get('policy')}, "
            f"first_seen_round={plan.get('first_seen_round')}, "
            f"last_seen_round={plan.get('last_seen_round')}, update_count={plan.get('update_count')}"
        )

        candidates = self._build_candidate_set()

        self._generate_enum_candidates(plan, candidates)
        self._generate_numeric_candidates(plan, candidates)
        self._generate_format_candidates(plan, candidates)
        self._generate_required_candidates(plan, candidates)
        self._generate_pattern_candidates(plan, candidates)
        self._generate_length_candidates(plan, candidates)
        self._generate_not_found_candidates(plan, candidates)
        self._finalize_string_candidates(plan, candidates)
        self._adapt_candidates_for_policy(plan, candidates)

        self._write_candidate_set(dictionary, plan, candidates)

    def _build_candidate_set(self) -> Dict[str, Any]:
        return {
            "valid_global": {key: [] for key in self.LIST_DICT_KEYS},
            "invalid_global": {key: [] for key in self.LIST_DICT_KEYS},
            "valid_payload_quoted": [],
            "valid_payload_unquoted": [],
        }

    def _generate_enum_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        if not plan["enum"]:
            return

        for item in plan["enum"]:
            if self._is_bool_string(item):
                self._add_bool_candidate(candidates, item, valid=True)
            elif self._looks_numeric(item):
                self._add_number_candidate(candidates, item, valid=True)
            else:
                self._add_string_candidate(candidates, item, valid=True)

        if not self.inject_invalid_values:
            return

        for item in self._enum_invalid_candidates(plan["enum"]):
            if self._is_bool_string(item):
                self._add_bool_candidate(candidates, item, valid=False)
            elif self._looks_numeric(item):
                self._add_number_candidate(candidates, item, valid=False)
            else:
                self._add_string_candidate(candidates, item, valid=False)

    def _generate_numeric_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        minimum = plan["minimum"]
        maximum = plan["maximum"]

        if minimum is None and maximum is None:
            return

        valid_values = []
        invalid_values = []

        if minimum is not None and maximum is not None and minimum <= maximum:
            step = max(self._numeric_step(minimum), self._numeric_step(maximum))
            midpoint = minimum + ((maximum - minimum) / 2.0)
            valid_values = [minimum, minimum + step, midpoint, maximum - step, maximum]
            invalid_values = [minimum - step, maximum + step]
        elif minimum is not None:
            step = self._numeric_step(minimum)
            valid_values = [minimum, minimum + step, minimum + (2 * step)]
            invalid_values = [minimum - step]
        elif maximum is not None:
            step = self._numeric_step(maximum)
            valid_values = [maximum - (2 * step), maximum - step, maximum]
            invalid_values = [maximum + step]

        for value in valid_values:
            self._add_number_candidate(candidates, self._number_to_string(value), valid=True)

        if self.inject_invalid_values:
            for value in invalid_values:
                self._add_number_candidate(candidates, self._number_to_string(value), valid=False)

    def _generate_format_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        fmt = plan["format"]
        if not fmt:
            return

        valid_values, invalid_values, category = self._format_candidate_sets(fmt)
        for value in valid_values:
            self._add_typed_candidate(candidates, category, value, valid=True)

        if self.inject_invalid_values:
            for value in invalid_values:
                self._add_typed_candidate(candidates, category, value, valid=False)

    def _generate_required_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        if not plan["required"]:
            return

        string_values, raw_values, bool_values = self._required_candidates(plan["parameter"])

        for value in string_values:
            self._add_string_candidate(candidates, value, valid=True)
        for value in raw_values:
            self._add_number_candidate(candidates, value, valid=True)
        for value in bool_values:
            self._add_bool_candidate(candidates, value, valid=True)

    def _generate_pattern_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        pattern = plan["pattern"]
        if not pattern:
            return

        valid_values, invalid_values = self._pattern_candidate_sets(pattern, plan["parameter"])
        for value in valid_values:
            self._add_string_candidate(candidates, value, valid=True)

        if self.inject_invalid_values:
            for value in invalid_values:
                self._add_string_candidate(candidates, value, valid=False)

    def _generate_length_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        min_length = plan["minLength"]
        max_length = plan["maxLength"]

        if min_length is None and max_length is None:
            return

        base = self._base_token(plan["parameter"])
        valid_lengths = []
        invalid_lengths = []

        if min_length is not None:
            valid_lengths.extend([min_length, min_length + 1])
            invalid_lengths.append(max(0, min_length - 1))

        if max_length is not None:
            valid_lengths.extend([max(0, max_length - 1), max_length])
            invalid_lengths.append(max_length + 1)

        if min_length is not None and max_length is not None and min_length <= max_length:
            midpoint = min_length + ((max_length - min_length) // 2)
            valid_lengths.append(midpoint)

        for current_length in valid_lengths:
            self._add_string_candidate(candidates, self._string_with_length(base, current_length), valid=True)

        if self.inject_invalid_values:
            for current_length in invalid_lengths:
                self._add_string_candidate(candidates, self._string_with_length(base, current_length), valid=False)

    def _generate_not_found_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        if not plan["not_found"] or not self.inject_invalid_values:
            return

        for value in ["-1", "0", "999999"]:
            self._add_number_candidate(candidates, value, valid=False)

    def _finalize_string_candidates(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        min_length = plan["minLength"]
        max_length = plan["maxLength"]
        pattern = plan["pattern"]
        valid_strings = candidates["valid_global"]["restler_fuzzable_string"]
        payload_strings = candidates["valid_payload_quoted"]

        filtered_global = []
        for value in valid_strings:
            if self._is_valid_string_candidate(value, min_length, max_length, pattern):
                self._append_unique(filtered_global, value)
        candidates["valid_global"]["restler_fuzzable_string"] = filtered_global

        filtered_payload = []
        for value in payload_strings:
            if self._is_valid_string_candidate(value, min_length, max_length, pattern):
                self._append_unique(filtered_payload, value)
        candidates["valid_payload_quoted"] = filtered_payload

    def _adapt_candidates_for_policy(self, plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        policy = str(plan.get("policy", "balance"))
        if policy == "exploit":
            for key in candidates["invalid_global"]:
                candidates["invalid_global"][key] = []
            self._trim_candidate_map(candidates["valid_global"], limit=5)
            candidates["valid_payload_quoted"] = self._trim_list(candidates["valid_payload_quoted"], 5)
            candidates["valid_payload_unquoted"] = self._trim_list(candidates["valid_payload_unquoted"], 5)
            return

        if policy == "explore":
            base = self._base_token(plan["parameter"])
            self._add_string_candidate(candidates, f"{base}-explore", valid=True)
            if self.inject_invalid_values:
                self._add_string_candidate(candidates, f"{base}@@@", valid=False)
                self._add_number_candidate(candidates, "999999", valid=False)
            self._trim_candidate_map(candidates["valid_global"], limit=2)
            self._trim_candidate_map(candidates["invalid_global"], limit=4)
            candidates["valid_payload_quoted"] = self._trim_list(candidates["valid_payload_quoted"], 2)
            candidates["valid_payload_unquoted"] = self._trim_list(candidates["valid_payload_unquoted"], 2)
            return

        self._trim_candidate_map(candidates["valid_global"], limit=4)
        self._trim_candidate_map(candidates["invalid_global"], limit=2)
        candidates["valid_payload_quoted"] = self._trim_list(candidates["valid_payload_quoted"], 4)
        candidates["valid_payload_unquoted"] = self._trim_list(candidates["valid_payload_unquoted"], 4)

    def _is_valid_string_candidate(
        self,
        value: str,
        min_length: Optional[int],
        max_length: Optional[int],
        pattern: Optional[str],
    ) -> bool:
        if min_length is not None and len(value) < min_length:
            return False
        if max_length is not None and len(value) > max_length:
            return False
        if pattern:
            try:
                if re.fullmatch(pattern, value) is None:
                    return False
            except re.error:
                return True
        return True

    def _trim_candidate_map(self, values: Dict[str, List[Any]], limit: int) -> None:
        for key, arr in values.items():
            values[key] = self._trim_list(arr, limit)

    def _trim_list(self, values: List[Any], limit: int) -> List[Any]:
        if limit <= 0:
            return []
        trimmed = []
        for value in values:
            if value in trimmed:
                continue
            trimmed.append(value)
            if len(trimmed) >= limit:
                break
        return trimmed

    def _write_candidate_set(self, dictionary: Dict[str, Any], plan: Dict[str, Any], candidates: Dict[str, Any]) -> None:
        write_to_global = not self._has_parameter_specific_candidates(candidates)

        if write_to_global:
            for key, values in candidates["valid_global"].items():
                self._append_many(dictionary[key], values)

            if self.inject_invalid_values:
                for key, values in candidates["invalid_global"].items():
                    self._append_many(dictionary[key], values)

        if candidates["valid_payload_quoted"]:
            self._append_parameter_candidates(
                dictionary,
                plan["parameter"],
                plan["location"],
                candidates["valid_payload_quoted"],
                quoted=True,
            )

        if candidates["valid_payload_unquoted"]:
            self._append_parameter_candidates(
                dictionary,
                plan["parameter"],
                plan["location"],
                candidates["valid_payload_unquoted"],
                quoted=False,
            )

    def _has_parameter_specific_candidates(self, candidates: Dict[str, Any]) -> bool:
        return bool(candidates["valid_payload_quoted"] or candidates["valid_payload_unquoted"])

    def _add_typed_candidate(
        self,
        candidates: Dict[str, Any],
        category: str,
        value: str,
        valid: bool,
    ) -> None:
        if category == "bool":
            self._add_bool_candidate(candidates, value, valid)
        elif category == "uuid":
            target = "valid_global" if valid else "invalid_global"
            self._append_unique(candidates[target]["restler_fuzzable_uuid4"], value)
            self._append_unique(candidates[target]["restler_fuzzable_string"], value)
            if valid:
                self._append_unique(candidates["valid_payload_quoted"], value)
        elif category == "date":
            target = "valid_global" if valid else "invalid_global"
            self._append_unique(candidates[target]["restler_fuzzable_date"], value)
            self._append_unique(candidates[target]["restler_fuzzable_string"], value)
            if valid:
                self._append_unique(candidates["valid_payload_quoted"], value)
        elif category == "datetime":
            target = "valid_global" if valid else "invalid_global"
            self._append_unique(candidates[target]["restler_fuzzable_datetime"], value)
            self._append_unique(candidates[target]["restler_fuzzable_string"], value)
            if valid:
                self._append_unique(candidates["valid_payload_quoted"], value)
        else:
            self._add_string_candidate(candidates, value, valid)

    def _add_string_candidate(self, candidates: Dict[str, Any], value: str, valid: bool) -> None:
        target = "valid_global" if valid else "invalid_global"
        self._append_unique(candidates[target]["restler_fuzzable_string"], value)
        if valid:
            self._append_unique(candidates["valid_payload_quoted"], value)

    def _add_number_candidate(self, candidates: Dict[str, Any], value: str, valid: bool) -> None:
        target = "valid_global" if valid else "invalid_global"
        self._append_unique(candidates[target]["restler_fuzzable_number"], value)
        if self._looks_int_string(value):
            self._append_unique(candidates[target]["restler_fuzzable_int"], value)
        if valid:
            self._append_unique(candidates["valid_payload_unquoted"], value)

    def _add_bool_candidate(self, candidates: Dict[str, Any], value: str, valid: bool) -> None:
        normalized = str(value).lower()
        target = "valid_global" if valid else "invalid_global"
        self._append_unique(candidates[target]["restler_fuzzable_bool"], normalized)
        if valid:
            self._append_unique(candidates["valid_payload_unquoted"], normalized)

    def _append_parameter_candidates(
        self,
        dictionary: Dict[str, Any],
        parameter: str,
        location: str,
        values: List[str],
        quoted: bool,
    ) -> None:
        key = self._parameter_payload_key(location, quoted)
        payload_map = dictionary.setdefault(key, {})

        if parameter not in payload_map or not isinstance(payload_map[parameter], list):
            payload_map[parameter] = []

        for value in values:
            self._append_unique(payload_map[parameter], value)

    def _parameter_payload_key(self, location: str, quoted: bool) -> str:
        normalized = (location or "unknown").lower()

        if normalized == "query":
            return "restler_custom_payload_query" if quoted else "restler_custom_payload_query_unquoted"

        if normalized == "header":
            return "restler_custom_payload_header" if quoted else "restler_custom_payload_header_unquoted"

        return "restler_custom_payload" if quoted else "restler_custom_payload_unquoted"

    def _required_candidates(self, parameter: str) -> Tuple[List[str], List[str], List[str]]:
        name = (parameter or "").lower()

        if "email" in name:
            return ["test@example.com"], [], []
        if "uuid" in name:
            return ["566048da-ed19-4cd3-8e0a-b7e0e1ec4d72"], [], []
        if name.endswith("id") or name.endswith("_id") or name == "id":
            return [], ["1", "2"], []
        if "date" in name and "time" not in name:
            return ["2024-01-01"], [], []
        if "time" in name:
            return ["2024-01-01T00:00:00Z"], [], []
        if name.startswith("is_") or name.startswith("has_") or name.startswith("can_") or name.startswith("enable_"):
            return [], [], ["true", "false"]

        base = self._base_token(parameter)
        return [base, f"{base}-value"], [], []

    def _enum_invalid_candidates(self, enum_values: List[str]) -> List[str]:
        if not enum_values:
            return []

        first = enum_values[0]
        if self._is_bool_string(first):
            return []
        if self._looks_numeric(first):
            number = self._to_number(first)
            if number is None:
                return []
            step = self._numeric_step(number)
            return [self._number_to_string(number + (10 * step))]
        return [f"{first}-invalid"]

    def _pattern_candidate_sets(self, pattern: str, parameter: str) -> Tuple[List[str], List[str]]:
        normalized = pattern.strip()
        known_patterns = {
            r"^[a-z0-9-]+$": (["abc-123", "restler-demo"], ["ABC!", "demo_value"]),
            r"^[A-Za-z0-9_]+$": (["user_01", "Demo123"], ["bad-value!", "with space"]),
            r"^\d+$": (["123", "2024"], ["abc", "12x"]),
            r"^[A-Fa-f0-9]+$": (["DEADBEEF", "12ab34"], ["not-hex", "xyz"]),
            r"^[a-z]+$": (["abc", "restler"], ["ABC", "abc1"]),
            r"^[A-Z]+$": (["ABC", "RESTLER"], ["abc", "AB1"]),
        }

        if normalized in known_patterns:
            return known_patterns[normalized]

        base = self._base_token(parameter)
        if "a-z0-9-" in normalized.lower():
            return [f"{base}-123"], [f"{base}_invalid"]
        if "a-za-z0-9_" in normalized.lower() or "a-za-z0-9" in normalized.lower():
            return [f"{base}_01", "Demo123"], [f"{base}-invalid"]
        if r"\d" in normalized or "[0-9]" in normalized:
            return ["12345"], [f"{base}abc"]

        return [], []

    def _format_candidate_sets(self, fmt: str) -> Tuple[List[str], List[str], str]:
        mapping = {
            "email": (
                ["test@example.com", "user+restler@example.org"],
                ["invalid-email", "missing-at.example.com"],
                "string",
            ),
            "uuid": (
                ["566048da-ed19-4cd3-8e0a-b7e0e1ec4d72", "00000000-0000-0000-0000-000000000000"],
                ["invalid-uuid"],
                "uuid",
            ),
            "date": (
                ["2024-01-01", "2030-06-15"],
                ["2024/01/01", "invalid-date"],
                "date",
            ),
            "date-time": (
                ["2024-01-01T00:00:00Z", "2030-06-15T08:30:00Z"],
                ["2024-01-01 00:00:00", "invalid-datetime"],
                "datetime",
            ),
            "datetime": (
                ["2024-01-01T00:00:00Z", "2030-06-15T08:30:00Z"],
                ["2024-01-01 00:00:00", "invalid-datetime"],
                "datetime",
            ),
            "uri": (
                ["https://example.com", "http://localhost/api"],
                ["not-a-uri", "ftp//broken"],
                "string",
            ),
            "url": (
                ["https://example.com", "http://localhost/api"],
                ["not-a-url", "ftp//broken"],
                "string",
            ),
            "ipv4": (
                ["127.0.0.1", "192.168.1.10"],
                ["999.999.999.999", "abc.def.ghi.jkl"],
                "string",
            ),
            "ipv6": (
                ["2001:db8::1", "::1"],
                ["invalid-ipv6", "12345"],
                "string",
            ),
            "hostname": (
                ["api.example.com", "localhost"],
                ["bad host", "host!name"],
                "string",
            ),
            "phone": (
                ["13800138000", "+1-202-555-0100"],
                ["abc-phone", "123-@@@"],
                "string",
            ),
        }
        return mapping.get(fmt, ([], [], "string"))

    def _base_token(self, parameter: str) -> str:
        token = re.sub(r"[^a-zA-Z0-9]+", "-", parameter or "value").strip("-").lower()
        return token or "value"

    def _string_with_length(self, token: str, length: int) -> str:
        if length <= 0:
            return ""
        repeated = token or "x"
        return (repeated * ((length // len(repeated)) + 1))[:length]

    def _normalize_scalar(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    def _to_number(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None

    def _to_int(self, value: Any) -> Optional[int]:
        try:
            numeric = float(value)
        except Exception:
            return None
        if not numeric.is_integer():
            return None
        return int(numeric)

    def _numeric_step(self, value: float) -> float:
        if float(value).is_integer():
            return 1.0

        raw = f"{value}".rstrip("0")
        if "." not in raw:
            return 1.0
        decimals = len(raw.split(".")[1])
        return 10 ** (-decimals)

    def _number_to_string(self, value: float) -> str:
        if float(value).is_integer():
            return str(int(round(value)))
        return format(value, "g")

    def _looks_numeric(self, value: str) -> bool:
        try:
            float(value)
            return True
        except Exception:
            return False

    def _looks_int_string(self, value: str) -> bool:
        return re.fullmatch(r"-?\d+", value or "") is not None

    def _is_bool_string(self, value: str) -> bool:
        return str(value).lower() in {"true", "false"}

    def _append_many(self, arr: List[Any], values: List[Any]) -> None:
        for value in values:
            self._append_unique(arr, value)

    def _append_unique(self, arr: List[Any], value: Any) -> None:
        if value not in arr:
            arr.append(value)

    def _deduplicate_all(self, dictionary: Dict[str, Any]) -> None:
        for key, value in dictionary.items():
            if isinstance(value, list):
                dictionary[key] = self._deduplicate_list(value)
            elif isinstance(value, dict):
                for inner_key, inner_value in value.items():
                    if isinstance(inner_value, list):
                        value[inner_key] = self._deduplicate_list(inner_value)

    def _deduplicate_list(self, values: List[Any]) -> List[Any]:
        seen = []
        for item in values:
            if item not in seen:
                seen.append(item)
        return seen


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--constraints", default="error_logs/semantic_constraints.jsonl")
    parser.add_argument("--base_dict", default="dict.json")
    parser.add_argument("--output_dict", default="dict_enhanced.json")
    parser.add_argument("--memory", default=None)
    parser.add_argument("--current_round", type=int, default=None)
    parser.add_argument("--reset_memory", action="store_true")
    parser.add_argument("--applied_constraints", default=None)
    parser.add_argument("--belief_threshold_high", type=float, default=0.75)
    parser.add_argument("--belief_threshold_low", type=float, default=0.40)
    args = parser.parse_args()

    updater = DictUpdater(
        min_confidence=0.80,
        min_count=1,
        enable_not_found=False,
        inject_invalid_values=True,
    )

    updater.run(
        constraints_path=args.constraints,
        base_dict_path=args.base_dict,
        output_dict_path=args.output_dict,
        memory_path=args.memory,
        current_round=args.current_round,
        reset_memory=args.reset_memory,
        applied_constraints_path=args.applied_constraints,
        belief_threshold_high=args.belief_threshold_high,
        belief_threshold_low=args.belief_threshold_low,
    )


if __name__ == "__main__":
    main()
