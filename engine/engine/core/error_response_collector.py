# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Collects LLM-oriented error response artifacts from a RESTler run."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List, Optional

if __package__ is None or __package__ == "":
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine.core import request_utilities
from engine.transport_layer.response import CONNECTION_CLOSED_CODE
from engine.transport_layer.response import RESTLER_INVALID_CODE
from engine.transport_layer.response import TIMEOUT_CODE
from engine.transport_layer.response import VALID_CODES


OUTPUT_DIR_NAME = "llm_error_responses"
OUTPUT_FILENAME = "error_responses.json"
SCHEMA_VERSION = "1.0"

MAX_TEXT_LENGTH = 4000
MAX_STRING_VALUE_LENGTH = 500
MAX_JSON_DEPTH = 4
MAX_LIST_ITEMS = 10
MAX_DICT_ITEMS = 30

SEMANTIC_KEYS = {
    "code",
    "message",
    "error",
    "errors",
    "details",
    "detail",
    "target",
    "reason",
    "title",
    "type",
    "status",
    "traceId",
    "requestId",
    "errorCode",
    "errorMessage",
    "description",
}


def collect_error_responses(experiment_dir: str, fuzzing_mode: Optional[str] = None) -> Dict[str, Any]:
    """Collects error responses from the specified experiment directory.

    @param experiment_dir: The current experiment directory.
    @type  experiment_dir: Str
    @param fuzzing_mode: The run mode for the current execution.
    @type  fuzzing_mode: Str or None

    @return: Summary of collected artifacts.
    @rtype : Dict
    """
    summary = {
        "schema_version": SCHEMA_VERSION,
        "experiment_dir": experiment_dir,
        "fuzzing_mode": fuzzing_mode,
        "output_dir": None,
        "output_file": None,
        "total_records": 0,
        "sources": {},
        "files": [],
    }

    if not experiment_dir or not os.path.isdir(experiment_dir):
        return summary

    output_dir = os.path.join(experiment_dir, OUTPUT_DIR_NAME)
    os.makedirs(output_dir, exist_ok=True)
    summary["output_dir"] = output_dir

    run_mode_label = _get_run_mode_label(fuzzing_mode)
    records: List[Dict[str, Any]] = []
    records.extend(_collect_test_mode_errors(experiment_dir))
    records.extend(_collect_bug_bucket_errors(experiment_dir, run_mode_label))

    source_counts: Dict[str, int] = {}
    filtered_records: List[Dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not _is_http_error_response(record.get("response", {}).get("status_code")):
            continue

        record["id"] = f"error_{index:04d}"
        source_name = record.get("source", {}).get("artifact_type", "unknown")
        source_counts[source_name] = source_counts.get(source_name, 0) + 1
        filtered_records.append(record)

    _cleanup_output_dir(output_dir)

    consolidated_payload = {
        "schema_version": SCHEMA_VERSION,
        "experiment_dir": experiment_dir,
        "fuzzing_mode": fuzzing_mode,
        "output_purpose": "LLM error-response analysis",
        "error_filter": "HTTP status_code >= 400",
        "sources": source_counts,
        "total_records": len(filtered_records),
        "records": filtered_records,
    }

    output_path = os.path.join(output_dir, OUTPUT_FILENAME)
    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(consolidated_payload, output_file, indent=2, ensure_ascii=False)

    summary["output_file"] = output_path
    summary["total_records"] = len(filtered_records)
    summary["sources"] = source_counts
    summary["files"] = [OUTPUT_FILENAME]
    return summary


def _collect_test_mode_errors(experiment_dir: str) -> List[Dict[str, Any]]:
    speccov_path = os.path.join(experiment_dir, "logs", "speccov-min.json")
    if not os.path.isfile(speccov_path):
        return []

    try:
        with open(speccov_path, "r", encoding="utf-8") as speccov_file:
            speccov = json.load(speccov_file)
    except Exception as error:
        _log_warning(f"Failed to load smoke test coverage log for LLM error collection: {error!s}")
        return []

    records: List[Dict[str, Any]] = []
    for entry_key, entry in speccov.items():
        if entry.get("valid", 1):
            continue

        request = _parse_http_request(entry.get("request"))
        response = _parse_http_response(entry.get("response"))
        record = _build_record(
            mode="test",
            artifact_type="speccov",
            artifact_path=speccov_path,
            request=request,
            response=response,
            sequence_context={
                "matching_prefix": entry.get("matching_prefix", []),
                "request_key": entry_key,
            },
            metadata={
                "valid": entry.get("valid"),
                "error_message": _truncate_text(entry.get("response")),
            },
        )
        records.append(record)

    return records


def _collect_bug_bucket_errors(experiment_dir: str, run_mode_label: str) -> List[Dict[str, Any]]:
    bug_buckets_dir = os.path.join(experiment_dir, "bug_buckets")
    if not os.path.isdir(bug_buckets_dir):
        return []

    records: List[Dict[str, Any]] = []
    for filename in sorted(os.listdir(bug_buckets_dir)):
        if not filename.endswith(".json"):
            continue
        if filename in {"Bugs.json", "bug_buckets.json"}:
            continue

        bug_path = os.path.join(bug_buckets_dir, filename)
        try:
            with open(bug_path, "r", encoding="utf-8") as bug_file:
                bug_detail = json.load(bug_file)
        except Exception as error:
            _log_warning(f"Failed to load bug bucket json for LLM error collection: {bug_path}; {error!s}")
            continue

        request_sequence = bug_detail.get("request_sequence", [])
        if not request_sequence:
            continue

        last_request = request_sequence[-1]
        request = _parse_http_request(last_request.get("replay_request"))
        response = _parse_http_response(last_request.get("response"))
        record = _build_record(
            mode=run_mode_label,
            artifact_type="bug_bucket",
            artifact_path=bug_path,
            request=request,
            response=response,
            sequence_context={
                "request_count": len(request_sequence),
                "requests": _summarize_request_sequence(request_sequence),
            },
            metadata={
                "checker_name": bug_detail.get("checker_name"),
                "reproducible": bug_detail.get("reproducible"),
                "endpoint": bug_detail.get("endpoint"),
                "verb": bug_detail.get("verb"),
                "bug_bucket_status_code": bug_detail.get("status_code"),
                "bug_bucket_status_text": bug_detail.get("status_text"),
            },
        )
        records.append(record)

    return records


def _build_record(mode: str,
                  artifact_type: str,
                  artifact_path: str,
                  request: Dict[str, Any],
                  response: Dict[str, Any],
                  sequence_context: Optional[Dict[str, Any]] = None,
                  metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response_body_json = response.get("body_json")
    semantic_summary = _extract_semantic_summary(response_body_json, response.get("body_text"))

    return {
        "id": None,
        "source": {
            "mode": mode,
            "artifact_type": artifact_type,
            "artifact_path": artifact_path,
        },
        "request": {
            "method": request.get("method"),
            "endpoint": request.get("endpoint"),
            "content_type": _get_header_value(request.get("headers", []), "Content-Type"),
            "body_text": request.get("body_text"),
            "body_json": request.get("body_json"),
        },
        "response": {
            "status_code": response.get("status_code"),
            "status_text": response.get("status_text"),
            "content_type": _get_header_value(response.get("headers", []), "Content-Type"),
            "body_text": response.get("body_text"),
            "body_json": response.get("body_json"),
        },
        "sequence_context": sequence_context or {},
        "semantic_summary": semantic_summary,
        "fingerprint": {
            "method_endpoint": _compact_method_endpoint(request.get("method"), request.get("endpoint")),
            "status_code": response.get("status_code"),
            "status_class": _status_code_class(response.get("status_code")),
            "semantic_signature": _build_semantic_signature(request, response, semantic_summary),
        },
        "metadata": metadata or {},
    }


def _parse_http_request(request_text: Optional[str]) -> Dict[str, Any]:
    parsed_request = request_utilities.parse_request_text(request_text)
    return {
        "raw": _truncate_text(parsed_request.get("raw")),
        "method": parsed_request.get("method"),
        "endpoint": parsed_request.get("endpoint"),
        "headers": parsed_request.get("headers", []),
        "body_text": _truncate_text(parsed_request.get("body_text")),
        "body_json": _summarize_json(parsed_request.get("body_json")),
    }


def _parse_http_response(response_text: Optional[str]) -> Dict[str, Any]:
    parsed_response = request_utilities.parse_response_text(response_text)
    return {
        "raw": _truncate_text(parsed_response.get("raw")),
        "status_code": parsed_response.get("status_code"),
        "status_text": parsed_response.get("status_text"),
        "headers": parsed_response.get("headers", []),
        "body_text": _truncate_text(parsed_response.get("body_text")),
        "body_json": _summarize_json(parsed_response.get("body_json")),
    }


def _summarize_request_sequence(request_sequence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summarized: List[Dict[str, Any]] = []
    for request in request_sequence:
        request_info = _parse_http_request(request.get("replay_request"))
        response_info = _parse_http_response(request.get("response"))
        summarized.append(
            {
                "method": request_info.get("method"),
                "endpoint": request_info.get("endpoint"),
                "response_status_code": response_info.get("status_code"),
                "response_status_text": response_info.get("status_text"),
                "response_body_preview": response_info.get("body_text"),
                "producer_timing_delay": request.get("producer_timing_delay"),
                "max_async_wait_time": request.get("max_async_wait_time"),
            }
        )
    return summarized


def _extract_semantic_summary(body_json: Any, body_text: Optional[str]) -> Dict[str, Any]:
    semantic_fields = {}
    if isinstance(body_json, dict):
        semantic_fields = _collect_semantic_fields(body_json)

    return {
        "top_level_keys": list(body_json.keys()) if isinstance(body_json, dict) else [],
        "semantic_fields": semantic_fields,
        "body_looks_like_json": body_json is not None,
        "body_preview": _truncate_text(body_text),
    }


def _collect_semantic_fields(data: Dict[str, Any], path: str = "") -> Dict[str, Any]:
    collected: Dict[str, Any] = {}
    for key, value in data.items():
        current_path = f"{path}.{key}" if path else key
        if key in SEMANTIC_KEYS:
            collected[current_path] = _summarize_json(value, depth=0)

        if isinstance(value, dict):
            nested = _collect_semantic_fields(value, current_path)
            collected.update(nested)
        elif isinstance(value, list):
            for idx, item in enumerate(value[:MAX_LIST_ITEMS]):
                if isinstance(item, dict):
                    nested = _collect_semantic_fields(item, f"{current_path}[{idx}]")
                    collected.update(nested)
    return collected


def _summarize_json(value: Any, depth: int = 0) -> Any:
    if value is None:
        return None

    if depth >= MAX_JSON_DEPTH:
        if isinstance(value, dict):
            return {"_truncated": True, "type": "object", "keys": list(value.keys())[:MAX_DICT_ITEMS]}
        if isinstance(value, list):
            return {"_truncated": True, "type": "array", "length": len(value)}
        if isinstance(value, str):
            return _truncate_text(value, MAX_STRING_VALUE_LENGTH)
        return value

    if isinstance(value, dict):
        summarized = {}
        items = list(value.items())
        for key, item_value in items[:MAX_DICT_ITEMS]:
            summarized[key] = _summarize_json(item_value, depth + 1)
        if len(items) > MAX_DICT_ITEMS:
            summarized["_truncated_keys"] = len(items) - MAX_DICT_ITEMS
        return summarized

    if isinstance(value, list):
        summarized_list = [_summarize_json(item, depth + 1) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            summarized_list.append({"_truncated_items": len(value) - MAX_LIST_ITEMS})
        return summarized_list

    if isinstance(value, str):
        return _truncate_text(value, MAX_STRING_VALUE_LENGTH)

    return value


def _truncate_text(text: Optional[str], limit: int = MAX_TEXT_LENGTH) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}...[truncated {omitted} chars]"


def _compact_method_endpoint(method: Optional[str], endpoint: Optional[str]) -> Optional[str]:
    if not method and not endpoint:
        return None
    if not method:
        return endpoint
    if not endpoint:
        return method
    return f"{method} {endpoint}"


def _status_code_class(status_code: Optional[str]) -> str:
    if status_code in VALID_CODES:
        return "valid"
    if status_code == TIMEOUT_CODE:
        return "timeout"
    if status_code == CONNECTION_CLOSED_CODE:
        return "connection_closed"
    if status_code == RESTLER_INVALID_CODE:
        return "sequence_invalid"
    if status_code and len(status_code) == 3 and status_code.isdigit():
        return f"{status_code[0]}xx"
    return "unknown"


def _build_semantic_signature(request: Dict[str, Any],
                              response: Dict[str, Any],
                              semantic_summary: Dict[str, Any]) -> str:
    method_endpoint = _compact_method_endpoint(request.get("method"), request.get("endpoint")) or "unknown_request"
    status_code = response.get("status_code") or "unknown_status"
    semantic_fields = semantic_summary.get("semantic_fields", {})
    if semantic_fields:
        semantic_hint = next(iter(semantic_fields.keys()))
    else:
        semantic_hint = "no_semantic_key"
    return f"{method_endpoint}|{status_code}|{semantic_hint}"


def _is_http_error_response(status_code: Optional[str]) -> bool:
    if not status_code or not status_code.isdigit():
        return False
    return int(status_code) >= 400


def _get_run_mode_label(fuzzing_mode: Optional[str]) -> str:
    if fuzzing_mode in {"directed-smoke-test", "test-all-combinations"}:
        return "test"
    return "fuzz"


def _get_header_value(headers: List[str], header_name: str) -> Optional[str]:
    return request_utilities.get_header_value(headers, header_name)


def _cleanup_output_dir(output_dir: str) -> None:
    for filename in os.listdir(output_dir):
        path = os.path.join(output_dir, filename)
        if os.path.isfile(path):
            os.remove(path)


def _log_warning(message: str) -> None:
    try:
        import utils.logger as runtime_logger
        runtime_logger.write_to_main(message)
    except Exception:
        print(message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment_dir", required=True, help="RESTler experiment directory to process.")
    parser.add_argument("--fuzzing_mode", required=False, default=None, help="Optional run mode label.")
    args = parser.parse_args()

    result = collect_error_responses(args.experiment_dir, args.fuzzing_mode)
    print(json.dumps(result, indent=2, ensure_ascii=False))
