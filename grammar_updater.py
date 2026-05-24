#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple


PrimitiveSpec = Dict[str, Any]
ContextInfo = Tuple[str, str]


class GrammarUpdater:
    """
    Rewrite RESTler grammar primitives so fields with parameter-specific
    dictionary candidates use custom payload primitives in the next round.
    """

    CUSTOM_PAYLOAD_RULES = [
        ("restler_custom_payload_query_unquoted", "query", False, "restler_custom_payload_query"),
        ("restler_custom_payload_query", "query", True, "restler_custom_payload_query"),
        ("restler_custom_payload_unquoted", "body", False, "restler_custom_payload"),
        ("restler_custom_payload", "body", True, "restler_custom_payload"),
        ("restler_custom_payload_header_unquoted", "header", False, "restler_custom_payload_header"),
        ("restler_custom_payload_header", "header", True, "restler_custom_payload_header"),
    ]
    NON_REPLACEABLE_PRIMITIVES = {
        "primitives.restler_fuzzable_group(",
    }
    PROTECTED_QUERY_PARAMETERS = {
        "context",
        "orderby",
        "_embed",
        "force",
        "slug",
        "order",
        "_fields",
        "page",
        "per_page",
        "offset",
        "search",
        "status",
    }

    def __init__(self, base_grammar_path: str, dict_path: str, output_grammar_path: str):
        self.base_grammar_path = os.path.abspath(base_grammar_path)
        self.dict_path = os.path.abspath(dict_path)
        self.output_grammar_path = os.path.abspath(output_grammar_path)
        self.lookup: Dict[Tuple[str, str], PrimitiveSpec] = {}

    def update(self) -> List[Dict[str, Any]]:
        grammar_lines = self._read_lines(self.base_grammar_path)
        grammar_lines = self._strip_wp_nonce_headers(grammar_lines)
        dictionary = self._load_json(self.dict_path)
        self.lookup = self._build_lookup(dictionary)

        updated_lines: List[str] = []
        replacements: List[Dict[str, Any]] = []

        for index, line in enumerate(grammar_lines):
            if not self._is_replaceable_primitive_line(line):
                updated_lines.append(line)
                continue

            context = self._infer_context(grammar_lines, index)
            if not context:
                updated_lines.append(line)
                continue

            replacement = self._build_replacement_line(line, context)
            if replacement is None:
                updated_lines.append(line)
                continue

            updated_lines.append(replacement)
            replacements.append(
                {
                    "line": index + 1,
                    "parameter": context[1],
                    "location": context[0],
                    "replacement": replacement.strip(),
                }
            )

        os.makedirs(os.path.dirname(self.output_grammar_path), exist_ok=True)
        with open(self.output_grammar_path, "w", encoding="utf-8") as f:
            f.writelines(updated_lines)

        return replacements

    def _strip_wp_nonce_headers(self, grammar_lines: List[str]) -> List[str]:
        """
        WordPress Application Passwords use Basic Auth and must not send
        X-WP-Nonce. A fuzzed nonce makes WordPress reject write requests with
        rest_cookie_invalid_nonce before Basic Auth can help.
        """
        updated_lines: List[str] = []
        index = 0
        while index < len(grammar_lines):
            line = grammar_lines[index]
            if 'primitives.restler_static_string("X-WP-Nonce: ")' not in line:
                updated_lines.append(line)
                index += 1
                continue

            index += 1
            if index < len(grammar_lines) and "primitives.restler_fuzzable_" in grammar_lines[index]:
                index += 1
            if index < len(grammar_lines) and 'primitives.restler_static_string("\\r\\n")' in grammar_lines[index]:
                index += 1

        return updated_lines

    def _build_lookup(self, dictionary: Dict[str, Any]) -> Dict[Tuple[str, str], PrimitiveSpec]:
        lookup: Dict[Tuple[str, str], PrimitiveSpec] = {}

        for dict_key, location, quoted, primitive_name in self.CUSTOM_PAYLOAD_RULES:
            payload_map = dictionary.get(dict_key, {})
            if not isinstance(payload_map, dict):
                continue

            for parameter, values in payload_map.items():
                if not isinstance(values, list) or not values:
                    continue

                if location == "query":
                    quoted = False

                lookup[(location, str(parameter))] = {
                    "primitive_name": primitive_name,
                    "quoted": quoted,
                }

        return lookup

    def _infer_context(self, grammar_lines: List[str], line_index: int) -> Optional[ContextInfo]:
        start = max(0, line_index - 5)
        context_window = "".join(grammar_lines[start:line_index])

        body_matches = re.findall(r'"([^"\r\n]+)"\s*:\s*', context_window)
        if body_matches:
            return "body", body_matches[-1]

        query_matches = re.findall(r'([A-Za-z_][A-Za-z0-9_-]*)=', context_window)
        if query_matches:
            return "query", query_matches[-1]

        header_matches = re.findall(r'([A-Za-z0-9_-]+):\s*"?\s*$', context_window, flags=re.MULTILINE)
        if header_matches:
            return "header", header_matches[-1]

        return None

    def _build_replacement_line(self, original_line: str, context: ContextInfo) -> Optional[str]:
        location, parameter = context
        if self._is_protected_parameter(location, parameter):
            return None

        spec = self.lookup.get((location, parameter))
        if spec is None:
            return None

        indent = original_line[: len(original_line) - len(original_line.lstrip())]
        trailing_comma = "," if original_line.rstrip().endswith(",") else ""
        newline = "\n" if original_line.endswith("\n") else ""
        quoted_literal = "True" if spec["quoted"] else "False"

        return (
            f'{indent}primitives.{spec["primitive_name"]}("{parameter}", quoted={quoted_literal})'
            f"{trailing_comma}{newline}"
        )

    def _is_replaceable_primitive_line(self, line: str) -> bool:
        if "primitives.restler_fuzzable_" not in line:
            return False
        return not any(token in line for token in self.NON_REPLACEABLE_PRIMITIVES)

    def _is_protected_parameter(self, location: str, parameter: str) -> bool:
        if location != "query":
            return False
        return parameter in self.PROTECTED_QUERY_PARAMETERS

    def _read_lines(self, path: str) -> List[str]:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()

    def _load_json(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a round-specific RESTler grammar from a dictionary.")
    parser.add_argument("--base_grammar", required=True, help="Base RESTler grammar template.")
    parser.add_argument("--dict_file", required=True, help="Dictionary file for the current round.")
    parser.add_argument("--output_grammar", required=True, help="Path to write the updated grammar.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    updater = GrammarUpdater(
        base_grammar_path=args.base_grammar,
        dict_path=args.dict_file,
        output_grammar_path=args.output_grammar,
    )
    replacements = updater.update()
    print(
        f"[GrammarUpdater] generated grammar: {args.output_grammar} "
        f"(replacements={len(replacements)})"
    )


if __name__ == "__main__":
    main()
