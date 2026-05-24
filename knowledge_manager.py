import json
import os
from typing import Any, Dict, List, Optional, Tuple


class KnowledgeManager:
    def __init__(
        self,
        belief_threshold_high: float = 0.75,
        belief_threshold_low: float = 0.40,
        stale_round_window: int = 3,
    ):
        self.belief_threshold_high = belief_threshold_high
        self.belief_threshold_low = belief_threshold_low
        self.stale_round_window = stale_round_window
        self.route_level_error_markers = [
            "未找到匹配 url 和请求方式的路由",
            "未找到匹配 url 和请求方法的路由",
            "no route was found matching the url and request method",
            "no route was found matching the url",
            "rest_no_route",
        ]

    def load(self, path: str) -> List[Dict[str, Any]]:
        raw_items = self._read_jsonl(path)
        knowledge_items: List[Dict[str, Any]] = []
        for item in raw_items:
            knowledge_items.extend(self._normalize_loaded_item(item))

        normalized_items = [self._normalize_knowledge_item(item) for item in knowledge_items]
        normalized_items.sort(key=lambda item: item.get("knowledge_key", ""))
        return normalized_items

    def save(self, path: str, items: List[Dict[str, Any]]) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        serializable = [self._serialize_item(self._normalize_knowledge_item(item)) for item in items]
        serializable.sort(key=lambda item: item.get("knowledge_key", ""))
        with open(path, "w", encoding="utf-8") as f:
            for item in serializable:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def update_from_constraints(
        self,
        memory_path: str,
        constraints: Optional[List[Dict[str, Any]]] = None,
        constraints_path: Optional[str] = None,
        round_id: Optional[int] = None,
        reset_memory: bool = False,
    ) -> Dict[str, Any]:
        if reset_memory and os.path.exists(memory_path):
            os.remove(memory_path)

        observed_constraints = constraints if constraints is not None else self._read_jsonl(constraints_path)
        knowledge_items = self.load(memory_path)
        index = {item["knowledge_key"]: item for item in knowledge_items}
        family_map = self._build_family_map(knowledge_items)

        updated_keys = set()
        for constraint in observed_constraints:
            item = self._constraint_to_knowledge(constraint, round_id)
            key = item["knowledge_key"]
            family_key = self._family_key(item)

            if key not in index:
                index[key] = item
                family_map.setdefault(family_key, set()).add(key)
            else:
                index[key] = self._merge_observation(index[key], item, round_id)

            self._mark_conflicts(index, index[key], family_map.get(family_key, set()))
            index[key] = self._refresh_item(index[key], current_round=round_id)
            updated_keys.add(key)

        items = list(index.values())
        self.save(memory_path, items)
        return self.summarize(items, current_round=round_id, updated_keys=updated_keys)

    def apply_feedback(
        self,
        memory_path: str,
        applied_constraints_path: Optional[str],
        current_constraints: Optional[List[Dict[str, Any]]] = None,
        current_constraints_path: Optional[str] = None,
        current_success_summary: Optional[Dict[str, Any]] = None,
        previous_success_summary: Optional[Dict[str, Any]] = None,
        feedback_round: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not applied_constraints_path or not os.path.exists(applied_constraints_path):
            return self._empty_feedback_summary()

        applied_items = self._read_jsonl(applied_constraints_path)
        if not applied_items:
            return self._empty_feedback_summary()

        current_items = current_constraints if current_constraints is not None else self._read_jsonl(current_constraints_path)
        current_keys = set()
        current_families = set()
        for item in current_items:
            key = item.get("knowledge_key") or item.get("observation_key")
            if key:
                current_keys.add(str(key))
            elif self._looks_like_constraint_record(item):
                current_keys.add(self._constraint_key(item))
            if self._looks_like_constraint_record(item):
                current_families.add(self._family_key(item))

        knowledge_items = self.load(memory_path)
        index = {item["knowledge_key"]: item for item in knowledge_items}
        current_endpoint_results = self._extract_endpoint_results(current_success_summary)
        previous_endpoint_results = self._extract_endpoint_results(previous_success_summary)

        resolved = 0
        persistent = 0
        success_promoted = 0
        endpoint_valid_hits = 0
        stable_successes = 0
        regressions = 0
        touched = set()
        for applied in applied_items:
            key = str(applied.get("knowledge_key", "")).strip()
            if not key or key not in index or key in touched:
                continue

            item = index[key]
            endpoint_key = self._endpoint_result_key(applied, fallback=item)
            current_valid = self._endpoint_is_valid(current_endpoint_results, endpoint_key)
            previous_valid = self._endpoint_is_valid(previous_endpoint_results, endpoint_key)
            if key in current_keys:
                item["persistent_error_rounds"] = int(item.get("persistent_error_rounds", 0)) + 1
                persistent += 1
            elif self._family_key(item) in current_families:
                item["persistent_error_rounds"] = int(item.get("persistent_error_rounds", 0)) + 1
                item["conflict_count"] = int(item.get("conflict_count", 0)) + 1
                persistent += 1
            else:
                item["resolved_rounds"] = int(item.get("resolved_rounds", 0)) + 1
                resolved += 1
                if current_valid:
                    item["endpoint_success_rounds"] = int(item.get("endpoint_success_rounds", 0)) + 1
                    endpoint_valid_hits += 1
                    if previous_endpoint_results and not previous_valid:
                        item["success_rounds"] = int(item.get("success_rounds", 0)) + 1
                        item["last_success_round"] = feedback_round
                        success_promoted += 1
                    elif previous_valid:
                        item["stable_success_rounds"] = int(item.get("stable_success_rounds", 0)) + 1
                        stable_successes += 1

            if previous_valid and not current_valid:
                item["endpoint_regression_rounds"] = int(item.get("endpoint_regression_rounds", 0)) + 1
                regressions += 1

            item["last_feedback_round"] = feedback_round
            index[key] = self._refresh_item(item, current_round=feedback_round)
            touched.add(key)

        self.save(memory_path, list(index.values()))
        return {
            "feedback_applied": len(touched),
            "resolved": resolved,
            "persistent": persistent,
            "endpoint_valid_hits": endpoint_valid_hits,
            "success_promoted": success_promoted,
            "stable_successes": stable_successes,
            "regressions": regressions,
        }

    def summarize(
        self,
        items: List[Dict[str, Any]],
        current_round: Optional[int] = None,
        updated_keys: Optional[set] = None,
    ) -> Dict[str, Any]:
        status_distribution: Dict[str, int] = {}
        policy_distribution: Dict[str, int] = {}
        for item in items:
            refreshed = self._refresh_item(item, current_round=current_round)
            status = refreshed.get("status", "candidate")
            policy = self.choose_policy(refreshed)
            status_distribution[status] = status_distribution.get(status, 0) + 1
            policy_distribution[policy] = policy_distribution.get(policy, 0) + 1

        return {
            "knowledge_count": len(items),
            "updated_knowledge_count": len(updated_keys or []),
            "status_distribution": status_distribution,
            "policy_distribution": policy_distribution,
            "confirmed_count": status_distribution.get("confirmed", 0),
            "conflicted_count": status_distribution.get("conflicted", 0),
            "stale_count": status_distribution.get("stale", 0),
            "success_backed_count": sum(1 for item in items if int(item.get("success_rounds", 0) or 0) > 0),
        }

    def choose_policy(self, item: Dict[str, Any]) -> str:
        belief = float(item.get("belief_score", 0.0))
        status = str(item.get("status", "candidate"))
        success_rounds = int(item.get("success_rounds", 0) or 0)

        if status in {"conflicted", "stale", "invalid"} or belief < self.belief_threshold_low:
            return "explore"
        if belief >= self.belief_threshold_high and success_rounds > 0:
            return "exploit"
        return "balance"

    def _normalize_loaded_item(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._looks_like_constraint_record(item):
            return [item]

        converted: List[Dict[str, Any]] = []
        metadata = {
            "first_seen_round": self._to_int(item.get("first_seen_round")),
            "last_seen_round": self._to_int(item.get("last_seen_round")),
            "observation_rounds": int(item.get("update_count", 0) or 0),
            "total_observations": int(item.get("total_count", 0) or 0),
            "source_rule_hits": int(item.get("total_count", 0) or 0),
            "source_llm_hits": 0,
            "persistent_error_rounds": 0,
            "resolved_rounds": 0,
            "success_rounds": 0,
            "endpoint_success_rounds": 0,
            "stable_success_rounds": 0,
            "endpoint_regression_rounds": 0,
            "last_success_round": None,
            "conflict_count": 0,
            "max_confidence": float(item.get("max_confidence", 0.0) or 0.0),
            "last_error_signature": item.get("last_error_signature"),
            "last_evidence_text": item.get("last_evidence_text"),
        }

        for constraint_type in ["minimum", "maximum", "minLength", "maxLength", "format", "pattern"]:
            value = item.get(constraint_type)
            if value is None or value == "":
                continue
            converted.append(self._legacy_plan_to_constraint(item, constraint_type, value, metadata))

        enum_values = item.get("enum") or []
        if enum_values:
            converted.append(self._legacy_plan_to_constraint(item, "enum", enum_values, metadata))

        if item.get("required"):
            converted.append(self._legacy_plan_to_constraint(item, "required", True, metadata))

        if item.get("not_found"):
            converted.append(self._legacy_plan_to_constraint(item, "not_found", True, metadata))

        return converted

    def _legacy_plan_to_constraint(
        self,
        item: Dict[str, Any],
        constraint_type: str,
        constraint_value: Any,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = {
            "endpoint": item.get("endpoint", ""),
            "method": item.get("method", ""),
            "parameter": item.get("parameter", ""),
            "location": item.get("location", "unknown"),
            "constraint_type": constraint_type,
            "constraint_value": constraint_value,
            "confidence": float(item.get("max_confidence", 0.8) or 0.8),
            "source": "rule",
            "source_strength": float(item.get("max_confidence", 0.8) or 0.8),
            "count": int(item.get("total_count", 1) or 1),
            "rule_name": "legacy_memory_import",
            "round_id": metadata.get("last_seen_round"),
            "observation_key": self._constraint_key(
                {
                    "endpoint": item.get("endpoint", ""),
                    "method": item.get("method", ""),
                    "parameter": item.get("parameter", ""),
                    "location": item.get("location", "unknown"),
                    "constraint_type": constraint_type,
                    "constraint_value": constraint_value,
                }
            ),
        }
        result.update(metadata)
        return result

    def _constraint_to_knowledge(self, constraint: Dict[str, Any], round_id: Optional[int]) -> Dict[str, Any]:
        item = {
            "endpoint": str(constraint.get("endpoint", "")),
            "method": str(constraint.get("method", "")),
            "parameter": str(constraint.get("parameter", "")),
            "location": str(constraint.get("location", "unknown")),
            "constraint_type": str(constraint.get("constraint_type", "unknown")),
            "constraint_value": constraint.get("constraint_value"),
            "knowledge_key": constraint.get("knowledge_key") or constraint.get("observation_key") or self._constraint_key(constraint),
            "first_seen_round": self._to_int(constraint.get("first_seen_round", round_id)),
            "last_seen_round": self._to_int(constraint.get("last_seen_round", round_id)),
            "observation_rounds": 1,
            "total_observations": int(constraint.get("count", 1) or 1),
            "source_rule_hits": int(constraint.get("count", 1) or 1) if constraint.get("source") == "rule" else 0,
            "source_llm_hits": int(constraint.get("count", 1) or 1) if constraint.get("source") == "llm" else 0,
            "persistent_error_rounds": 0,
            "resolved_rounds": 0,
            "success_rounds": 0,
            "endpoint_success_rounds": 0,
            "stable_success_rounds": 0,
            "endpoint_regression_rounds": 0,
            "last_success_round": None,
            "conflict_count": 0,
            "max_confidence": float(constraint.get("confidence", 0.0) or 0.0),
            "last_error_signature": constraint.get("error_signature"),
            "last_evidence_text": constraint.get("evidence_text") or constraint.get("evidence"),
            "last_source": constraint.get("source", "rule"),
            "last_rule_name": constraint.get("rule_name"),
            "last_status_code": constraint.get("status"),
            "source_strength_sum": float(constraint.get("source_strength", constraint.get("confidence", 0.0)) or 0.0),
        }
        return self._refresh_item(item, current_round=round_id)

    def _merge_observation(
        self,
        existing: Dict[str, Any],
        current: Dict[str, Any],
        round_id: Optional[int],
    ) -> Dict[str, Any]:
        merged = self._normalize_knowledge_item(existing)
        merged["total_observations"] = int(merged.get("total_observations", 0)) + int(current.get("total_observations", 0))
        merged["source_rule_hits"] = int(merged.get("source_rule_hits", 0)) + int(current.get("source_rule_hits", 0))
        merged["source_llm_hits"] = int(merged.get("source_llm_hits", 0)) + int(current.get("source_llm_hits", 0))
        merged["source_strength_sum"] = float(merged.get("source_strength_sum", 0.0)) + float(current.get("source_strength_sum", 0.0))
        merged["max_confidence"] = max(float(merged.get("max_confidence", 0.0)), float(current.get("max_confidence", 0.0)))
        merged["last_error_signature"] = current.get("last_error_signature") or merged.get("last_error_signature")
        merged["last_evidence_text"] = current.get("last_evidence_text") or merged.get("last_evidence_text")
        merged["last_source"] = current.get("last_source") or merged.get("last_source")
        merged["last_rule_name"] = current.get("last_rule_name") or merged.get("last_rule_name")
        merged["last_status_code"] = current.get("last_status_code") or merged.get("last_status_code")
        merged["last_seen_round"] = self._to_int(current.get("last_seen_round", round_id))
        if round_id is not None and merged.get("last_counted_round") != round_id:
            merged["observation_rounds"] = int(merged.get("observation_rounds", 0)) + 1
            merged["last_counted_round"] = round_id
        return merged

    def _mark_conflicts(
        self,
        index: Dict[str, Dict[str, Any]],
        current_item: Dict[str, Any],
        family_keys: set,
    ) -> None:
        for sibling_key in family_keys:
            if sibling_key == current_item["knowledge_key"] or sibling_key not in index:
                continue
            sibling = index[sibling_key]
            if self._value_signature(sibling.get("constraint_value")) == self._value_signature(current_item.get("constraint_value")):
                continue
            sibling["conflict_count"] = int(sibling.get("conflict_count", 0)) + 1
            current_item["conflict_count"] = int(current_item.get("conflict_count", 0)) + 1
            index[sibling_key] = self._refresh_item(sibling, current_round=current_item.get("last_seen_round"))

    def _refresh_item(self, item: Dict[str, Any], current_round: Optional[int]) -> Dict[str, Any]:
        normalized = self._normalize_knowledge_item(item)
        normalized["belief_score"] = round(self._compute_belief_score(normalized, current_round), 4)
        normalized["status"] = self._compute_status(normalized, current_round)
        normalized["policy"] = self.choose_policy(normalized)
        return normalized

    def _compute_belief_score(self, item: Dict[str, Any], current_round: Optional[int]) -> float:
        confidence = float(item.get("max_confidence", 0.0))
        constraint_type = str(item.get("constraint_type", "unknown"))
        resolved_rounds = int(item.get("resolved_rounds", 0) or 0)
        success_rounds = int(item.get("success_rounds", 0) or 0)
        persistent_rounds = float(item.get("persistent_error_rounds", 0))
        conflict_rounds = float(item.get("conflict_count", 0))
        regression_rounds = float(item.get("endpoint_regression_rounds", 0))
        evidence_bonus = min(0.20, 0.04 * float(item.get("observation_rounds", 0)))
        support_bonus = min(0.12, 0.02 * float(item.get("total_observations", 0)))
        source_bonus = min(0.10, 0.015 * float(item.get("source_rule_hits", 0)))
        llm_bonus = min(0.05, 0.01 * float(item.get("source_llm_hits", 0)))
        resolved_bonus = min(0.06, 0.02 * float(resolved_rounds))
        success_bonus = min(0.30, 0.12 * float(success_rounds))
        persistent_penalty = min(0.22, 0.05 * persistent_rounds)
        conflict_penalty = min(0.30, 0.10 * conflict_rounds)
        regression_penalty = min(0.24, 0.08 * regression_rounds)
        unresolved_constraint_penalty = 0.0
        dependency_penalty = 0.0
        route_level_penalty = 0.0

        if constraint_type in {"required", "not_found"} and success_rounds <= 0:
            unresolved_constraint_penalty = 0.18

        if constraint_type == "not_found" and str(item.get("location", "unknown")) == "path":
            dependency_penalty = 0.10

        if self._is_route_level_failure(item):
            route_level_penalty = 0.35

        stale_penalty = 0.0
        if current_round is not None and item.get("last_seen_round") is not None:
            rounds_since_seen = max(0, int(current_round) - int(item["last_seen_round"]))
            if rounds_since_seen >= self.stale_round_window:
                stale_penalty = min(0.20, 0.04 * rounds_since_seen)

        score = (
            0.25
            + (0.35 * confidence)
            + evidence_bonus
            + support_bonus
            + source_bonus
            + llm_bonus
            + resolved_bonus
            + success_bonus
            - persistent_penalty
            - conflict_penalty
            - regression_penalty
            - unresolved_constraint_penalty
            - dependency_penalty
            - route_level_penalty
            - stale_penalty
        )
        return max(0.05, min(0.99, score))

    def _compute_status(self, item: Dict[str, Any], current_round: Optional[int]) -> str:
        belief = float(item.get("belief_score", 0.0))
        success_rounds = int(item.get("success_rounds", 0) or 0)

        if self._is_route_level_failure(item):
            return "invalid"
        if int(item.get("conflict_count", 0)) >= 2:
            return "conflicted"

        if current_round is not None and item.get("last_seen_round") is not None:
            rounds_since_seen = max(0, int(current_round) - int(item["last_seen_round"]))
            if rounds_since_seen >= self.stale_round_window:
                return "stale"

        if belief >= self.belief_threshold_high and success_rounds > 0:
            return "confirmed"
        if belief >= self.belief_threshold_low:
            return "candidate"
        return "weak"

    def _is_route_level_failure(self, item: Dict[str, Any]) -> bool:
        status_code = str(item.get("last_status_code", "")).strip()
        evidence = self._normalize_text(
            item.get("last_evidence_text")
            or item.get("last_error_signature")
            or ""
        )
        if status_code != "404":
            return False
        return any(marker in evidence for marker in self.route_level_error_markers)

    def _normalize_text(self, value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    def _normalize_knowledge_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        normalized = {
            "endpoint": str(item.get("endpoint", "")),
            "method": str(item.get("method", "")),
            "parameter": str(item.get("parameter", "")),
            "location": str(item.get("location", "unknown")),
            "constraint_type": str(item.get("constraint_type", "unknown")),
            "constraint_value": item.get("constraint_value"),
            "knowledge_key": str(item.get("knowledge_key") or self._constraint_key(item)),
            "first_seen_round": self._to_int(item.get("first_seen_round")),
            "last_seen_round": self._to_int(item.get("last_seen_round")),
            "observation_rounds": int(item.get("observation_rounds", item.get("update_count", 0)) or 0),
            "total_observations": int(item.get("total_observations", item.get("total_count", 0)) or 0),
            "source_rule_hits": int(item.get("source_rule_hits", 0) or 0),
            "source_llm_hits": int(item.get("source_llm_hits", 0) or 0),
            "persistent_error_rounds": int(item.get("persistent_error_rounds", 0) or 0),
            "resolved_rounds": int(item.get("resolved_rounds", 0) or 0),
            "success_rounds": int(item.get("success_rounds", 0) or 0),
            "endpoint_success_rounds": int(item.get("endpoint_success_rounds", 0) or 0),
            "stable_success_rounds": int(item.get("stable_success_rounds", 0) or 0),
            "endpoint_regression_rounds": int(item.get("endpoint_regression_rounds", 0) or 0),
            "conflict_count": int(item.get("conflict_count", 0) or 0),
            "max_confidence": float(item.get("max_confidence", item.get("confidence", 0.0)) or 0.0),
            "last_error_signature": item.get("last_error_signature"),
            "last_evidence_text": item.get("last_evidence_text"),
            "last_source": item.get("last_source"),
            "last_rule_name": item.get("last_rule_name"),
            "last_status_code": item.get("last_status_code"),
            "last_feedback_round": self._to_int(item.get("last_feedback_round")),
            "last_success_round": self._to_int(item.get("last_success_round")),
            "source_strength_sum": float(item.get("source_strength_sum", item.get("source_strength", 0.0)) or 0.0),
            "belief_score": float(item.get("belief_score", 0.0) or 0.0),
            "status": item.get("status"),
            "policy": item.get("policy"),
            "last_counted_round": self._to_int(item.get("last_counted_round", item.get("last_seen_round"))),
        }
        if normalized["first_seen_round"] is None:
            normalized["first_seen_round"] = normalized["last_seen_round"]
        return normalized

    def _serialize_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        serializable = dict(item)
        return serializable

    def _constraint_key(self, item: Dict[str, Any]) -> str:
        return "||".join(
            [
                str(item.get("endpoint", "")),
                str(item.get("method", "")),
                str(item.get("parameter", "")),
                str(item.get("location", "unknown")),
                str(item.get("constraint_type", "unknown")),
                self._value_signature(item.get("constraint_value")),
            ]
        )

    def _family_key(self, item: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
        return (
            str(item.get("endpoint", "")),
            str(item.get("method", "")),
            str(item.get("parameter", "")),
            str(item.get("location", "unknown")),
            str(item.get("constraint_type", "unknown")),
        )

    def _build_family_map(self, items: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str, str, str], set]:
        family_map: Dict[Tuple[str, str, str, str, str], set] = {}
        for item in items:
            family_map.setdefault(self._family_key(item), set()).add(item["knowledge_key"])
        return family_map

    def _value_signature(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _looks_like_constraint_record(self, item: Dict[str, Any]) -> bool:
        return "constraint_type" in item and "constraint_value" in item

    def _empty_feedback_summary(self) -> Dict[str, Any]:
        return {
            "feedback_applied": 0,
            "resolved": 0,
            "persistent": 0,
            "endpoint_valid_hits": 0,
            "success_promoted": 0,
            "stable_successes": 0,
            "regressions": 0,
        }

    def _extract_endpoint_results(self, summary: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        if not summary:
            return {}
        endpoint_results = summary.get("endpoint_results")
        if not isinstance(endpoint_results, dict):
            return {}
        return {str(key): value for key, value in endpoint_results.items()}

    def _endpoint_result_key(self, item: Dict[str, Any], fallback: Optional[Dict[str, Any]] = None) -> str:
        method = str(item.get("method") or (fallback or {}).get("method") or "").strip().upper()
        endpoint = str(item.get("endpoint") or (fallback or {}).get("endpoint") or "").strip()
        return f"{method} {endpoint}".strip()

    def _endpoint_is_valid(self, endpoint_results: Dict[str, Dict[str, Any]], endpoint_key: str) -> bool:
        if not endpoint_key:
            return False
        result = endpoint_results.get(endpoint_key)
        if not isinstance(result, dict):
            return False
        raw_valid = result.get("valid", 0)
        if isinstance(raw_valid, bool):
            return raw_valid
        if isinstance(raw_valid, (int, float)):
            return int(raw_valid) == 1
        return str(raw_valid).strip().lower() in {"1", "true", "yes"}

    def _read_jsonl(self, path: Optional[str]) -> List[Dict[str, Any]]:
        if not path or not os.path.exists(path):
            return []

        results = []
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

    def _to_int(self, value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except Exception:
            return None
