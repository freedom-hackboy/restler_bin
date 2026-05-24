# semantic_extractor.py
# -*- coding: utf-8 -*-

import os
import re
import json
from typing import Any, Dict, List, Optional, Tuple

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class SemanticExtractor:
    """
    错误语义提取模块
    输入: restler_error_summary.jsonl
    输出: semantic_constraints.jsonl

    流程:
    1. 规则提取
    2. 规则提取不到时，LLM 补充提取
    3. 结果归一化
    4. 相同约束合并
    """

    def __init__(self,
                 use_llm: bool = True,
                 model_name: str = "gpt-4o-mini",
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 llm_timeout_sec: Optional[float] = None):
        self.use_llm = use_llm
        self.model_name = model_name
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://yunwu.ai/v1")
        self.llm_timeout_sec = float(
            llm_timeout_sec
            if llm_timeout_sec is not None
            else os.environ.get("OPENAI_TIMEOUT_SEC", "45")
        )
        self.current_round: Optional[int] = None

        self.client = None
        if self.use_llm and OpenAI is not None and self.api_key:
            try:
                self.client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url
                )
            except Exception as e:
                print(f"[SemanticExtractor] LLM client init failed: {e}")
                self.client = None

    def extract_from_file(self, input_path: str, output_path: str, round_id: Optional[int] = None) -> None:
        """
        从 jsonl 文件读取错误记录，提取语义约束，输出到 jsonl
        """
        self.current_round = round_id
        records = self._read_jsonl(input_path)
        print(f"[SemanticExtractor] loaded {len(records)} error records from {input_path}")

        extracted_constraints = []

        for i, record in enumerate(records, 1):
            try:
                constraints = self.extract_constraints_from_record(record)
                if constraints:
                    extracted_constraints.extend(constraints)
            except Exception as e:
                print(f"[SemanticExtractor] record #{i} extract failed: {e}")

        merged_constraints = self._merge_constraints(extracted_constraints)
        self._write_jsonl(output_path, merged_constraints)

        print(f"[SemanticExtractor] extracted constraints: {len(extracted_constraints)}")
        print(f"[SemanticExtractor] merged constraints: {len(merged_constraints)}")
        print(f"[SemanticExtractor] saved to: {output_path}")

    def extract_constraints_from_record(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从单条错误记录中提取语义约束
        """
        endpoint = record.get("endpoint")
        method = record.get("method")
        params = record.get("params", {}) or {}
        status = record.get("status")
        error = record.get("error", "")
        count = record.get("count", 1)

        if not error:
            return []

        # 1. 优先规则提取
        rule_constraints = self._extract_by_rules(
            endpoint=endpoint,
            method=method,
            params=params,
            status=status,
            error=error,
            count=count
        )
        if rule_constraints:
            return rule_constraints

        # 2. 规则提取不到，再走 LLM
        llm_constraints = self._extract_by_llm(
            endpoint=endpoint,
            method=method,
            params=params,
            status=status,
            error=error,
            count=count
        )
        return llm_constraints

    # =========================
    # 规则提取
    # =========================

    def _extract_by_rules(self,
                          endpoint: str,
                          method: str,
                          params: Dict[str, Any],
                          status: Any,
                          error: str,
                          count: int) -> List[Dict[str, Any]]:
        error_text = self._normalize_error_text(error)
        if not error_text:
            return []

        rules = [
            ("minimum_rule", self._rule_minimum),
            ("maximum_rule", self._rule_maximum),
            ("enum_rule", self._rule_enum),
            ("required_rule", self._rule_required),
            ("non_empty_rule", self._rule_non_empty),
            ("length_rule", self._rule_length),
            ("format_rule", self._rule_format),
            ("pattern_rule", self._rule_pattern),
            ("not_found_rule", self._rule_not_found),
        ]

        constraints = []
        for rule_name, rule in rules:
            constraints.extend(rule(endpoint, method, params, status, error_text, count, rule_name=rule_name))

        return self._deduplicate_rule_constraints(constraints)

    def _rule_minimum(self, endpoint, method, params, status, error, count, rule_name="minimum_rule"):
        param_ref = self._param_ref()
        number_ref = self._number_ref()
        patterns = [
            rf"{param_ref}\s+(?:must|should|needs?\s+to|has\s+to)\s+be\s+(?:at\s+least|>=|greater\s+than\s+or\s+equal\s+to|more\s+than\s+or\s+equal\s+to|no\s+less\s+than)\s+(?P<value>{number_ref})",
            rf"(?:minimum|min)\s+(?:value|allowed|limit)?\s*(?:for|of)\s+{param_ref}\s+(?:is|should\s+be|must\s+be)\s+(?P<value>{number_ref})",
            rf"{param_ref}\s+(?:cannot|can't|must\s+not|should\s+not)\s+be\s+less\s+than\s+(?P<value>{number_ref})",
        ]

        results = self._build_numeric_rule_constraints(
            endpoint, method, params, status, error, count,
            patterns=patterns,
            constraint_type="minimum",
            confidence=0.95,
            rule_name=rule_name,
        )

        keyword_rules = [
            (rf"{param_ref}\s+(?:must|should)\s+be\s+positive\b", 1, 0.88),
            (rf"{param_ref}\s+(?:must|should)\s+be\s+non-negative\b", 0, 0.9),
        ]
        for pattern, value, confidence in keyword_rules:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                if not param:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="minimum",
                    constraint_value=value,
                    confidence=confidence,
                    source="rule",
                    rule_name=rule_name,
                ))

        return results

    def _rule_maximum(self, endpoint, method, params, status, error, count, rule_name="maximum_rule"):
        param_ref = self._param_ref()
        number_ref = self._number_ref()
        patterns = [
            rf"{param_ref}\s+(?:must|should|needs?\s+to|has\s+to)\s+be\s+(?:at\s+most|<=|less\s+than\s+or\s+equal\s+to|lower\s+than\s+or\s+equal\s+to|no\s+more\s+than)\s+(?P<value>{number_ref})",
            rf"{param_ref}\s+(?:cannot|can't|must\s+not|should\s+not)\s+(?:exceed|be\s+greater\s+than)\s+(?P<value>{number_ref})",
            rf"(?:maximum|max)\s+(?:value|allowed|limit)?\s*(?:for|of)\s+{param_ref}\s+(?:is|should\s+be|must\s+be)\s+(?P<value>{number_ref})",
            rf"{param_ref}\s+(?:must|should)\s+be\s+no\s+greater\s+than\s+(?P<value>{number_ref})",
        ]
        return self._build_numeric_rule_constraints(
            endpoint, method, params, status, error, count,
            patterns=patterns,
            constraint_type="maximum",
            confidence=0.95,
            rule_name=rule_name,
        )

    def _rule_enum(self, endpoint, method, params, status, error, count, rule_name="enum_rule"):
        param_ref = self._param_ref()
        patterns = [
            rf"{param_ref}\s+(?:must|should)\s+be\s+one\s+of[:\s]+(?P<values>.+?)(?:[.;]|$)",
            rf"{param_ref}\s+(?:must|should)\s+be\s+either[:\s]+(?P<values>.+?)(?:[.;]|$)",
            rf"{param_ref}\s+is\s+invalid[,;:]?\s*(?:allowed|valid|accepted|supported)\s+values?\s+(?:are|include)?[:\s]+(?P<values>.+?)(?:[.;]|$)",
            rf"(?:allowed|valid|accepted|supported)\s+values?\s+(?:for|of)\s+{param_ref}\s+(?:are|include)?[:\s]+(?P<values>.+?)(?:[.;]|$)",
            rf"expected\s+{param_ref}\s+to\s+be\s+one\s+of[:\s]+(?P<values>.+?)(?:[.;]|$)",
        ]

        results = []
        for pattern in patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                values = self._split_enum_values(match.group("values"))
                if not param or not values:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="enum",
                    constraint_value=values,
                    confidence=0.95,
                    source="rule",
                    rule_name=rule_name,
                ))

        return results

    def _rule_required(self, endpoint, method, params, status, error, count, rule_name="required_rule"):
        param_ref = self._param_ref()
        patterns = [
            rf"{param_ref}\s+(?:is|are)\s+required\b",
            rf"{param_ref}\s+(?:field|parameter|property)\s+is\s+required\b",
            rf"{param_ref}\s+is\s+mandatory\b",
            rf"missing\s+required\s+(?:field|parameter|property)[:\s]+{param_ref}",
            rf"required\s+(?:field|parameter|property)[:\s]+{param_ref}",
            rf"missing(?:\s+value)?\s+for\s+{param_ref}",
            rf"{param_ref}\s+is\s+missing\b",
            rf"must\s+provide\s+{param_ref}",
        ]

        results = []
        for pattern in patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                if not param:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="required",
                    constraint_value=True,
                    confidence=0.93,
                    source="rule",
                    rule_name=rule_name,
                ))

        return results

    def _rule_non_empty(self, endpoint, method, params, status, error, count, rule_name="non_empty_rule"):
        param_ref = self._param_ref()
        results = []

        null_patterns = [
            rf"{param_ref}\s+(?:cannot|can't|must\s+not|may\s+not|should\s+not)\s+be\s+null\b",
            rf"{param_ref}\s+cannot\s+be\s+none\b",
            rf"{param_ref}\s+must\s+not\s+be\s+null\b",
        ]
        for pattern in null_patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                if not param:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="required",
                    constraint_value=True,
                    confidence=0.9,
                    source="rule",
                    rule_name=rule_name,
                ))

        empty_patterns = [
            rf"{param_ref}\s+(?:cannot|can't|must\s+not|may\s+not|should\s+not)\s+be\s+(?:empty|blank)\b",
            rf"{param_ref}\s+cannot\s+be\s+an\s+empty\s+string\b",
            rf"{param_ref}\s+must\s+not\s+be\s+empty\b",
            rf"{param_ref}\s+must\s+not\s+be\s+blank\b",
            rf"{param_ref}\s+cannot\s+be\s+null\s+or\s+empty\b",
            rf"{param_ref}\s+cannot\s+be\s+empty\s+or\s+null\b",
        ]
        for pattern in empty_patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                if not param:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="minLength",
                    constraint_value=1,
                    confidence=0.9,
                    source="rule",
                    rule_name=rule_name,
                ))

                if re.search(r"null", match.group(0), re.I):
                    results.append(self._build_constraint(
                        endpoint, method, params, status, error, count,
                        parameter=param,
                        location=self._guess_location(param, params, endpoint),
                        constraint_type="required",
                        constraint_value=True,
                        confidence=0.88,
                        source="rule",
                        rule_name=rule_name,
                    ))

        return results

    def _rule_length(self, endpoint, method, params, status, error, count, rule_name="length_rule"):
        param_ref = self._param_ref()
        results = []

        patterns_between = [
            rf"length\s+of\s+{param_ref}\s+(?:must|should)\s+be\s+between\s+(?P<min_value>\d+)\s+and\s+(?P<max_value>\d+)",
            rf"{param_ref}\s+length\s+(?:must|should)\s+be\s+between\s+(?P<min_value>\d+)\s+and\s+(?P<max_value>\d+)",
            rf"{param_ref}\s+(?:must|should)\s+be\s+between\s+(?P<min_value>\d+)\s+and\s+(?P<max_value>\d+)\s+(?:characters?|chars?|bytes?|items?)",
        ]
        for pattern in patterns_between:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                min_value = self._parse_int(match.group("min_value"))
                max_value = self._parse_int(match.group("max_value"))
                if not param or min_value is None or max_value is None:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="minLength",
                    constraint_value=min_value,
                    confidence=0.94,
                    source="rule",
                    rule_name=rule_name,
                ))
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="maxLength",
                    constraint_value=max_value,
                    confidence=0.94,
                    source="rule",
                    rule_name=rule_name,
                ))

        patterns_min = [
            rf"length\s+of\s+{param_ref}\s+(?:must|should)\s+be\s+at\s+least\s+(?P<value>\d+)",
            rf"{param_ref}\s+(?:must|should)\s+have\s+at\s+least\s+(?P<value>\d+)\s+(?:characters?|chars?|bytes?|items?)",
            rf"{param_ref}\s+is\s+too\s+short.*?(?:minimum|min)\s+length\s+(?:is|should\s+be)\s+(?P<value>\d+)",
            rf"(?:minimum|min)\s+length\s+(?:for|of)\s+{param_ref}\s+(?:is|should\s+be|must\s+be)\s+(?P<value>\d+)",
            rf"{param_ref}\s+(?:must|should)\s+be\s+at\s+least\s+(?P<value>\d+)\s+(?:characters?|chars?|bytes?|items?)",
        ]
        for pattern in patterns_min:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                value = self._parse_int(match.group("value"))
                if not param or value is None:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="minLength",
                    constraint_value=value,
                    confidence=0.94,
                    source="rule",
                    rule_name=rule_name,
                ))

        patterns_max = [
            rf"length\s+of\s+{param_ref}\s+(?:must|should)\s+be\s+at\s+most\s+(?P<value>\d+)",
            rf"{param_ref}\s+(?:must|should)\s+have\s+at\s+most\s+(?P<value>\d+)\s+(?:characters?|chars?|bytes?|items?)",
            rf"{param_ref}\s+is\s+too\s+long.*?(?:maximum|max)\s+length\s+(?:is|should\s+be)\s+(?P<value>\d+)",
            rf"(?:maximum|max)\s+length\s+(?:for|of)\s+{param_ref}\s+(?:is|should\s+be|must\s+be)\s+(?P<value>\d+)",
            rf"{param_ref}\s+(?:must|should)\s+not\s+exceed\s+(?P<value>\d+)\s+(?:characters?|chars?|bytes?|items?)",
        ]
        for pattern in patterns_max:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                value = self._parse_int(match.group("value"))
                if not param or value is None:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="maxLength",
                    constraint_value=value,
                    confidence=0.94,
                    source="rule",
                    rule_name=rule_name,
                ))

        return results

    def _rule_format(self, endpoint, method, params, status, error, count, rule_name="format_rule"):
        param_ref = self._param_ref()
        format_ref = r"email|e-mail|uuid|guid|date-time|datetime|date|uri|url|ipv4|ipv6|hostname|host|phone(?:\s+number)?"
        patterns = [
            rf"{param_ref}\s+(?:must|should)\s+be\s+a?\s*valid\s+(?P<format>{format_ref})",
            rf"{param_ref}\s+is\s+not\s+a?\s*valid\s+(?P<format>{format_ref})",
            rf"invalid\s+(?P<format>{format_ref})\s+format\s+for\s+{param_ref}",
            rf"{param_ref}\s+has\s+invalid\s+(?P<format>{format_ref})\s+format",
            rf"{param_ref}\s+contains\s+an?\s+invalid\s+(?P<format>{format_ref})",
        ]

        results = []
        for pattern in patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                fmt = self._normalize_format_name(match.group("format"))
                if not param or not fmt:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="format",
                    constraint_value=fmt,
                    confidence=0.92,
                    source="rule",
                    rule_name=rule_name,
                ))

        return results

    def _rule_pattern(self, endpoint, method, params, status, error, count, rule_name="pattern_rule"):
        param_ref = self._param_ref()
        results = []

        explicit_patterns = [
            rf"{param_ref}\s+(?:must|should)\s+match\s+(?:the\s+)?(?:regular\s+expression|regex|pattern)[:\s]+(?P<pattern>[^.;]+)",
            rf"(?:pattern|regex)\s+(?:for|of)\s+{param_ref}\s+(?:is|should\s+be|must\s+be)[:\s]+(?P<pattern>[^.;]+)",
        ]
        for pattern in explicit_patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                raw_pattern = self._clean_pattern_value(match.group("pattern"))
                if not param or not raw_pattern:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="pattern",
                    constraint_value=raw_pattern,
                    confidence=0.9,
                    source="rule",
                    rule_name=rule_name,
                ))

        natural_language_patterns = [
            (rf"{param_ref}\s+(?:may|can|must|should)\s+only\s+contain\s+lowercase\s+letters,\s*numbers(?:,\s*| and )hyphens", r"^[a-z0-9-]+$"),
            (rf"only\s+lowercase\s+letters,\s*numbers(?:,\s*| and )hyphens\s+are\s+allowed\s+for\s+{param_ref}", r"^[a-z0-9-]+$"),
            (rf"{param_ref}\s+(?:may|can|must|should)\s+only\s+contain\s+letters,\s*numbers(?:,\s*| and )underscores", r"^[A-Za-z0-9_]+$"),
            (rf"only\s+letters,\s*numbers(?:,\s*| and )underscores\s+are\s+allowed\s+for\s+{param_ref}", r"^[A-Za-z0-9_]+$"),
            (rf"{param_ref}\s+(?:must|should)\s+be\s+alphanumeric\b", r"^[A-Za-z0-9]+$"),
            (rf"{param_ref}\s+(?:must|should)\s+contain\s+only\s+digits\b", r"^\d+$"),
            (rf"{param_ref}\s+(?:must|should)\s+be\s+hexadecimal\b", r"^[A-Fa-f0-9]+$"),
        ]
        for pattern, value in natural_language_patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                if not param:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type="pattern",
                    constraint_value=value,
                    confidence=0.88,
                    source="rule",
                    rule_name=rule_name,
                ))

        return results

    def _rule_not_found(self, endpoint, method, params, status, error, count, rule_name="not_found_rule"):
        if not re.search(r"\b(not\s+found|does\s+not\s+exist|no\s+.+\s+found)\b", error, re.I):
            return []

        param_ref = self._param_ref()
        patterns = [
            rf"(?P<resource>[a-zA-Z][a-zA-Z0-9_\-\s]*)\s+with\s+{param_ref}\s*=.*?(?:not\s+found|does\s+not\s+exist)",
            rf"(?P<resource>[a-zA-Z][a-zA-Z0-9_\-\s]*)\s+with\s+{param_ref}\s+.*?(?:was\s+)?not\s+found",
            rf"no\s+(?P<resource>[a-zA-Z][a-zA-Z0-9_\-\s]*)\s+found\s+for\s+{param_ref}",
            rf"(?P<resource>[a-zA-Z][a-zA-Z0-9_\-\s]*)\s+(?:was\s+)?not\s+found",
            rf"(?P<resource>[a-zA-Z][a-zA-Z0-9_\-\s]*)\s+does\s+not\s+exist",
        ]

        results = []
        for pattern in patterns:
            for match in re.finditer(pattern, error, re.I):
                resource_name = self._clean_resource_name(match.group("resource"))
                raw_param = match.groupdict().get("param")
                param = self._resolve_parameter_name(raw_param, params, endpoint, error)
                if not param:
                    param = self._guess_path_param(endpoint)
                confidence = 0.88 if str(status) == "404" else 0.78
                if not resource_name:
                    resource_name = self._resource_name_from_endpoint(endpoint)

                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint) if raw_param else "path",
                    constraint_type="not_found",
                    constraint_value={"resource": resource_name},
                    confidence=confidence,
                    source="rule",
                    rule_name=rule_name,
                ))

        return results

    # =========================
    # LLM 提取
    # =========================

    def _extract_by_llm(self,
                        endpoint: str,
                        method: str,
                        params: Dict[str, Any],
                        status: Any,
                        error: str,
                        count: int) -> List[Dict[str, Any]]:
        if not self.use_llm or self.client is None:
            return []

        prompt = f"""
你是一个 REST API 错误语义约束提取器。
请根据以下错误记录，提取可以用于 API 测试增强的参数约束。

输入记录：
endpoint: {endpoint}
method: {method}
params: {json.dumps(params, ensure_ascii=False)}
status: {status}
error: {error}
count: {count}

请只返回 JSON 数组，不要返回任何解释。
每个元素格式如下：
{{
  "parameter": "参数名",
  "location": "query|path|body|header|unknown",
  "constraint_type": "minimum|maximum|enum|required|minLength|maxLength|format|pattern|not_found|unknown",
  "constraint_value": 具体值,
  "confidence": 0到1之间的小数
}}

要求：
1. 只提取从错误信息中可以 reasonably 推断的约束
2. 如果无法确定参数名，尽量结合 params 和 endpoint 推断
3. 如果完全无法确定，则返回空数组 []
4. constraint_value 必须是结构化值，例如：
   - minimum: 2
   - enum: ["draft", "published"]
   - required: true
   - format: "email"
"""

        try:
            resp = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "你是一个严谨的 API 错误约束提取器，只输出 JSON。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                timeout=self.llm_timeout_sec
            )

            content = resp.choices[0].message.content.strip()
            data = self._safe_parse_json(content)

            if not isinstance(data, list):
                return []

            results = []
            for item in data:
                if not isinstance(item, dict):
                    continue

                parameter = item.get("parameter", "unknown")
                location = item.get("location", "unknown")
                constraint_type = item.get("constraint_type", "unknown")
                constraint_value = item.get("constraint_value")
                confidence = float(item.get("confidence", 0.7))

                if constraint_type == "unknown":
                    continue

                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=parameter,
                    location=location,
                    constraint_type=constraint_type,
                    constraint_value=constraint_value,
                    confidence=confidence,
                    source="llm",
                    rule_name="llm_semantic_extractor",
                ))
            return results

        except Exception as e:
            print(f"[SemanticExtractor] LLM extract failed: {e}")
            return []

    # =========================
    # 约束构造 / 合并
    # =========================

    def _build_constraint(self,
                          endpoint: str,
                          method: str,
                          params: Dict[str, Any],
                          status: Any,
                          error: str,
                          count: int,
                          parameter: str,
                          location: str,
                          constraint_type: str,
                          constraint_value: Any,
                          confidence: float,
                          source: str,
                          rule_name: Optional[str] = None) -> Dict[str, Any]:
        error_signature = self._build_error_signature(error, parameter)
        return {
            "endpoint": endpoint,
            "method": method,
            "parameter": parameter,
            "location": location,
            "constraint_type": constraint_type,
            "constraint_value": constraint_value,
            "evidence": error,
            "evidence_text": error[:240],
            "error_signature": error_signature,
            "status": status,
            "source": source,
            "rule_name": rule_name or ("llm_semantic_extractor" if source == "llm" else "rule_heuristic"),
            "source_strength": round(confidence if source == "llm" else max(confidence, 0.9), 4),
            "round_id": self.current_round,
            "observation_key": self._constraint_observation_key(
                endpoint=endpoint,
                method=method,
                parameter=parameter,
                location=location,
                constraint_type=constraint_type,
                constraint_value=constraint_value,
            ),
            "count": count,
            "confidence": round(confidence, 4)
        }

    def _merge_constraints(self, constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = {}

        for c in constraints:
            key = (
                c.get("endpoint"),
                c.get("method"),
                c.get("parameter"),
                c.get("location"),
                c.get("constraint_type"),
                json.dumps(c.get("constraint_value"), sort_keys=True, ensure_ascii=False)
            )

            if key not in merged:
                merged[key] = dict(c)
            else:
                merged[key]["count"] = merged[key].get("count", 1) + c.get("count", 1)
                merged[key]["confidence"] = max(
                    merged[key].get("confidence", 0.0),
                    c.get("confidence", 0.0)
                )

        return list(merged.values())

    # =========================
    # 辅助函数
    # =========================

    def _normalize_error_text(self, error: str) -> str:
        return re.sub(r"\s+", " ", (error or "")).strip()

    def _build_error_signature(self, error: str, parameter: Optional[str]) -> str:
        signature = self._normalize_error_text(error).lower()
        if parameter:
            signature = re.sub(rf"(?<![a-zA-Z0-9_]){re.escape(parameter.lower())}(?![a-zA-Z0-9_])", "PARAM", signature)
        signature = re.sub(r"[\"'][^\"']+[\"']", "STR", signature)
        signature = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27}\b", "UUID", signature)
        signature = re.sub(r"-?\d+(?:\.\d+)?", "NUM", signature)
        signature = re.sub(r"\s+", " ", signature).strip()
        return signature[:200]

    def _constraint_observation_key(
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

    def _param_ref(self) -> str:
        return r"(?:field|parameter|property|path\s+parameter|query\s+parameter|body\s+parameter|header)?\s*[`'\"$]?(?P<param>[a-zA-Z_][a-zA-Z0-9_\-.\[\]]*)[`'\"]?"

    def _number_ref(self) -> str:
        return r"-?\d+(?:\.\d+)?"

    def _build_numeric_rule_constraints(self,
                                        endpoint: str,
                                        method: str,
                                        params: Dict[str, Any],
                                        status: Any,
                                        error: str,
                                        count: int,
                                        patterns: List[str],
                                        constraint_type: str,
                                        confidence: float,
                                        rule_name: Optional[str] = None) -> List[Dict[str, Any]]:
        results = []
        for pattern in patterns:
            for match in re.finditer(pattern, error, re.I):
                param = self._resolve_parameter_name(match.group("param"), params, endpoint, error)
                value = self._parse_number(match.group("value"))
                if not param or value is None:
                    continue
                results.append(self._build_constraint(
                    endpoint, method, params, status, error, count,
                    parameter=param,
                    location=self._guess_location(param, params, endpoint),
                    constraint_type=constraint_type,
                    constraint_value=value,
                    confidence=confidence,
                    source="rule",
                    rule_name=rule_name,
                ))
        return results

    def _resolve_parameter_name(self,
                                raw_parameter: Optional[str],
                                params: Dict[str, Any],
                                endpoint: str,
                                error: str) -> Optional[str]:
        normalized = self._normalize_parameter_name(raw_parameter)
        if normalized:
            return normalized
        return self._guess_parameter_from_context(error, params, endpoint)

    def _normalize_parameter_name(self, raw_parameter: Optional[str]) -> Optional[str]:
        if not raw_parameter:
            return None

        value = str(raw_parameter).strip().strip("`'\"")
        value = re.sub(
            r"^(?:field|parameter|property|path\s+parameter|query\s+parameter|body\s+parameter)\s+",
            "",
            value,
            flags=re.I
        )
        value = value.lstrip("$.")
        value = re.sub(r"\[(?:'|\")?([a-zA-Z_][a-zA-Z0-9_\-]*)(?:'|\")?\]", r".\1", value)
        value = re.sub(r"\[\d+\]", "", value)
        value = value.strip(" .:,;()[]{}")

        if not value:
            return None

        if "." in value:
            value = value.split(".")[-1]

        value = value.strip(" .:,;()[]{}")
        if not value:
            return None

        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_\-]*$", value):
            return None

        return value

    def _guess_location(self, parameter: str, params: Dict[str, Any], endpoint: str) -> str:
        alias_map = self._build_param_alias_map(params, endpoint)

        for alias in self._parameter_aliases(parameter):
            if alias in alias_map:
                return alias_map[alias][1]

        if endpoint and ("{" + parameter + "}") in endpoint:
            return "path"

        if parameter.lower() in ["authorization", "token", "apikey", "api_key"]:
            return "header"

        return "unknown"

    def _guess_parameter_from_context(self,
                                      error: str,
                                      params: Dict[str, Any],
                                      endpoint: str) -> Optional[str]:
        alias_map = self._build_param_alias_map(params, endpoint)
        if not alias_map:
            return None

        lower_error = error.lower()
        matched = []
        for alias, (canonical, _) in alias_map.items():
            if re.search(rf"(?<![a-zA-Z0-9_]){re.escape(alias)}(?![a-zA-Z0-9_])", lower_error):
                matched.append((alias, canonical))

        if matched:
            matched.sort(key=lambda item: len(item[0]), reverse=True)
            return matched[0][1]

        canonical_names = []
        for canonical, _ in alias_map.values():
            if canonical not in canonical_names:
                canonical_names.append(canonical)
        if len(canonical_names) == 1:
            return canonical_names[0]

        return None

    def _build_param_alias_map(self, params: Dict[str, Any], endpoint: str) -> Dict[str, Tuple[str, str]]:
        alias_map = {}

        if not isinstance(params, dict):
            params = {}

        for location in ["query", "body", "path", "header"]:
            for raw_name, raw_location in self._iter_param_entries(params.get(location, {}), location):
                canonical = self._normalize_parameter_name(raw_name)
                if not canonical:
                    continue
                for alias in self._parameter_aliases(raw_name):
                    alias_map[alias] = (canonical, raw_location)

        for path_param in self._extract_path_params(endpoint):
            canonical = self._normalize_parameter_name(path_param)
            if not canonical:
                continue
            for alias in self._parameter_aliases(path_param):
                alias_map[alias] = (canonical, "path")

        return alias_map

    def _iter_param_entries(self, data: Any, location: str, prefix: str = ""):
        if isinstance(data, dict):
            for key, value in data.items():
                current = f"{prefix}.{key}" if prefix else str(key)
                yield current, location
                yield from self._iter_param_entries(value, location, current)
        elif isinstance(data, list):
            for item in data:
                yield from self._iter_param_entries(item, location, prefix)

    def _parameter_aliases(self, raw_parameter: str) -> List[str]:
        aliases = []
        raw_value = str(raw_parameter).strip()

        for candidate in [raw_value, self._normalize_parameter_name(raw_value)]:
            if not candidate:
                continue

            normalized = str(candidate).strip().lower().strip("`'\"")
            normalized = re.sub(r"\[\d+\]", "", normalized)
            normalized = normalized.strip(" .:,;()[]{}")
            if not normalized:
                continue

            for alias in [normalized, normalized.split(".")[-1]]:
                alias = alias.strip(" .:,;()[]{}")
                if alias and alias not in aliases:
                    aliases.append(alias)

        return aliases

    def _extract_path_params(self, endpoint: Optional[str]) -> List[str]:
        if not endpoint:
            return []
        return re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_\-]*)\}", endpoint)

    def _guess_path_param(self, endpoint: str) -> str:
        path_params = self._extract_path_params(endpoint)
        if path_params:
            return path_params[0]
        return "id"

    def _split_enum_values(self, raw: str) -> List[str]:
        raw = self._normalize_error_text(raw)
        raw = raw.strip().strip(".")
        raw = re.sub(r"^(?:\[|\(|\{)\s*", "", raw)
        raw = re.sub(r"\s*(?:\]|\)|\})$", "", raw)
        raw = re.sub(r"\s+(?:but|received|got|provided)\s+.+$", "", raw, flags=re.I)

        quoted = re.findall(r"[\"']([^\"']+)[\"']", raw)
        if quoted:
            return [value.strip() for value in quoted if value.strip()]

        raw = raw.replace(" or ", ",")
        raw = raw.replace(" and ", ",")
        raw = raw.replace("|", ",")
        parts = [x.strip(" '\"") for x in raw.split(",")]
        return [x for x in parts if x and len(x) < 80]

    def _normalize_format_name(self, raw_format: Optional[str]) -> Optional[str]:
        if not raw_format:
            return None
        value = self._normalize_error_text(raw_format).lower()
        mapping = {
            "e-mail": "email",
            "guid": "uuid",
            "datetime": "date-time",
            "host": "hostname",
            "phone number": "phone",
        }
        return mapping.get(value, value)

    def _clean_pattern_value(self, raw_pattern: Optional[str]) -> Optional[str]:
        if not raw_pattern:
            return None
        value = self._normalize_error_text(raw_pattern).strip("`'\" ")
        value = re.sub(r"\s+(?:but|received|got|provided)\s+.+$", "", value, flags=re.I)
        value = value.strip(" .")
        if not value:
            return None
        return value

    def _clean_resource_name(self, resource: Optional[str]) -> Optional[str]:
        if not resource:
            return None
        value = self._normalize_error_text(resource).strip(" .")
        value = re.sub(r"\b(with|for)\b.*$", "", value, flags=re.I).strip(" .")
        if not value:
            return None
        return value

    def _resource_name_from_endpoint(self, endpoint: Optional[str]) -> str:
        if not endpoint:
            return "resource"
        parts = [part for part in str(endpoint).split("/") if part and not part.startswith("{")]
        if not parts:
            return "resource"
        return parts[-1]

    def _parse_number(self, raw_value: Optional[str]) -> Optional[Any]:
        if raw_value is None:
            return None
        value = str(raw_value).strip()
        try:
            number = float(value)
        except Exception:
            return None
        if number.is_integer():
            return int(number)
        return number

    def _parse_int(self, raw_value: Optional[str]) -> Optional[int]:
        number = self._parse_number(raw_value)
        if isinstance(number, int):
            return number
        return None

    def _deduplicate_rule_constraints(self, constraints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduplicated = {}
        for constraint in constraints:
            key = (
                constraint.get("parameter"),
                constraint.get("location"),
                constraint.get("constraint_type"),
                json.dumps(constraint.get("constraint_value"), sort_keys=True, ensure_ascii=False)
            )
            if key not in deduplicated or constraint.get("confidence", 0.0) > deduplicated[key].get("confidence", 0.0):
                deduplicated[key] = constraint
        return list(deduplicated.values())

    def _safe_parse_json(self, text: str):
        text = text.strip()
        try:
            return json.loads(text)
        except Exception:
            pass

        # 尝试提取 ```json ... ```
        m = re.search(r"```json\s*(.*?)\s*```", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass

        # 尝试提取 [ ... ]
        m = re.search(r"(\[.*\])", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass

        return []

    def _read_jsonl(self, path: str) -> List[Dict[str, Any]]:
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

    def _write_jsonl(self, path: str, data: List[Dict[str, Any]]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")


def main():
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="error_logs/restler_error_summary.jsonl")
    parser.add_argument("--output", default="error_logs/semantic_constraints.jsonl")
    parser.add_argument("--round_id", type=int, default=None)
    parser.add_argument("--disable_llm", action="store_true", help="Disable LLM extraction and use rule-based extraction only.")
    parser.add_argument("--llm_timeout_sec", type=float, default=None, help="Per-request LLM timeout in seconds.")
    args = parser.parse_args()

    extractor = SemanticExtractor(
        use_llm=not args.disable_llm,
        model_name="gpt-4o-mini",
        api_key=os.environ.get("OPENAI_API_KEY", "sk-47b7JeGr9heuVydLfaKPUUtNwuE9ZfLLPbfLtOk2aE5hq6nt"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://yunwu.ai/v1"),
        llm_timeout_sec=args.llm_timeout_sec,
    )

    extractor.extract_from_file(args.input, args.output, round_id=args.round_id)


if __name__ == "__main__":
    main()
