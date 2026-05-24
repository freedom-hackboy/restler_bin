import argparse
import json
import os
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import parse_qs, urlparse

from restler_settings import Settings

_lock = Lock()
_pipeline_instance = None


class SemanticFeedbackPipeline:
    def __init__(self):
        self._feedback_dir = self._init_feedback_dir()
        self.error_responses_path = os.path.join(self._feedback_dir, "error_responses.jsonl")

    def _init_feedback_dir(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "..", "..", "..", ".."))
        feedback_dir = os.path.join(project_root, "semantic_feedback")
        os.makedirs(feedback_dir, exist_ok=True)
        return feedback_dir

    def _parse_request(self, rendered_data):
        try:
            lines = rendered_data.split("\r\n")
            request_line = lines[0].split(" ")
            method = request_line[0]
            full_path = request_line[1]

            parsed = urlparse(full_path)
            query_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

            headers = {}
            body = ""
            if "" in lines:
                separator = lines.index("")
                header_lines = lines[1:separator]
                body = "".join(lines[separator + 1 :])
            else:
                header_lines = lines[1:]

            for header in header_lines:
                if ": " in header:
                    key, value = header.split(": ", 1)
                    headers[key] = value

            body_params = {}
            for pair in body.split("&"):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    body_params[key] = value

            return {
                "method": method,
                "path": parsed.path,
                "query": query_params,
                "headers": headers,
                "body": body,
                "body_params": body_params,
                "params": {**query_params, **body_params},
            }
        except Exception:
            return {
                "method": "",
                "path": "",
                "query": {},
                "headers": {},
                "body": "",
                "body_params": {},
                "params": {},
            }

    def _parse_response(self, response):
        body_text = getattr(response, "body", None) or getattr(response, "to_str", None) or str(response)
        json_body = getattr(response, "json_body", None)
        if json_body:
            try:
                parsed_json = json.loads(json_body)
            except Exception:
                parsed_json = None
        else:
            parsed_json = None

        return {
            "status_code": getattr(response, "status_code", None),
            "status_text": getattr(response, "status_text", None),
            "headers": getattr(response, "headers_dict", {}),
            "body_text": body_text,
            "json_body": parsed_json,
        }

    def collect(self, rendered_data, response):
        try:
            if not response:
                return

            status_code = getattr(response, "status_code", None)
            if status_code is None or int(status_code) < 400:
                return

            request_info = self._parse_request(rendered_data)
            response_info = self._parse_response(response)

            entry = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "fuzzing_mode": getattr(Settings(), "fuzzing_mode", None),
                "request": request_info,
                "response": response_info,
            }

            with _lock:
                with open(self.error_responses_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


def get_pipeline():
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = SemanticFeedbackPipeline()
    return _pipeline_instance


def _load_json_or_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        content = handle.read().strip()

    if not content:
        return []

    if content.startswith("["):
        return json.loads(content)

    if content.startswith("{") and "\n" not in content:
        return [json.loads(content)]

    return [json.loads(line) for line in content.splitlines() if line.strip()]


def _ensure_list_of_strings(values):
    if not isinstance(values, list):
        raise ValueError(f"Expected a list of values, got: {type(values)}")
    return [str(value) for value in values]


def merge_semantics_into_dict(dict_path, semantics_path, output_path=None):
    with open(dict_path, encoding="utf-8") as handle:
        mutations = json.load(handle)

    semantics = _load_json_or_jsonl(semantics_path)
    applied = 0

    for item in semantics:
        dict_target = item.get("dict_target", "restler_custom_payload")
        values = _ensure_list_of_strings(item.get("values", []))
        if not values:
            continue

        if dict_target == "restler_custom_payload":
            tag = item.get("tag") or item.get("parameter")
            if not tag:
                continue
            mutations.setdefault(dict_target, {})
            existing = mutations[dict_target].setdefault(tag, [])
            for value in values:
                if value not in existing:
                    existing.append(value)
                    applied += 1
        else:
            mutations.setdefault(dict_target, [])
            existing = mutations[dict_target]
            for value in values:
                if value not in existing:
                    existing.append(value)
                    applied += 1

    destination = output_path or dict_path
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(mutations, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    return {"output_path": destination, "applied_values": applied}


def _build_cli():
    parser = argparse.ArgumentParser(description="Collect RESTler error responses and merge LLM semantics into dict.json.")
    subparsers = parser.add_subparsers(dest="command")

    merge_parser = subparsers.add_parser("merge", help="Merge LLM-extracted semantics into a RESTler dictionary.")
    merge_parser.add_argument("--dict", dest="dict_path", required=True, help="Path to dict.json")
    merge_parser.add_argument("--semantics", dest="semantics_path", required=True, help="Path to JSON or JSONL semantics file")
    merge_parser.add_argument("--output", dest="output_path", required=False, help="Optional output path")

    return parser


if __name__ == "__main__":
    cli = _build_cli()
    args = cli.parse_args()

    if args.command == "merge":
        result = merge_semantics_into_dict(args.dict_path, args.semantics_path, args.output_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
