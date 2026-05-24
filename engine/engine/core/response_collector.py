# engine/core/response_collector.py

import json
import os
import re
from urllib.parse import urlparse, parse_qs
from threading import Lock

_lock = Lock()


class ResponseCollector:
    def __init__(self):
        self.log_path = self._init_log_path()

    # ⭐ 初始化日志路径（兼容所有 RESTler 版本）
    def _init_log_path(self):
        base_dir = "RestlerResults"

        try:
            if os.path.exists(base_dir):
                subdirs = sorted(os.listdir(base_dir))
                if subdirs:
                    log_dir = os.path.join(base_dir, subdirs[-1], "logs")
                else:
                    log_dir = os.path.join(base_dir, "logs")
            else:
                log_dir = "logs"
        except Exception:
            log_dir = "logs"

        os.makedirs(log_dir, exist_ok=True)

        path = os.path.join(log_dir, "semantic_logs.jsonl")
        print("[Collector] log path:", path)
        return path

    # ⭐ 解析请求（支持 GET query + POST body）
    def _parse_request(self, rendered_data):
        try:
            lines = rendered_data.split("\r\n")
            first = lines[0].split(" ")

            method = first[0]
            full_path = first[1]

            parsed = urlparse(full_path)
            endpoint = parsed.path

            # ✅ query 参数
            query_params = parse_qs(parsed.query)
            query_params = {k: v[0] for k, v in query_params.items()}

            # ✅ body 参数
            body = ""
            if "" in lines:
                idx = lines.index("")
                body = "".join(lines[idx+1:])

            body_params = {}
            for pair in body.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    body_params[k] = v

            params = {**query_params, **body_params}

            return method, endpoint, params

        except Exception as e:
            print("[Collector parse error]", e)
            return "", "", {}

    # ⭐ 提取语义（已修复你现在的核心问题🔥）
    def _extract_semantics(self, response):
        try:
            text = ""

            # ✅ 优先取 body（关键修复点）
            if hasattr(response, "body") and response.body:
                text = response.body
            elif hasattr(response, "to_str"):
                text = response.to_str()
            else:
                text = str(response)

            # ✅ 尝试解析 JSON（非常关键🔥）
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    for key in ["detail", "message", "error"]:
                        if key in data:
                            text = data[key]
                            break
            except:
                pass

            print("[DEBUG response text]:", text)

            # ✅ 语义规则
            patterns = [
                (r'([A-Za-z_]+) must be at least (\d+)', "min"),
                (r'([A-Za-z_]+) must be at most (\d+)', "max"),
                (r'([A-Za-z_]+) already exists', "unique"),
                (r'([A-Za-z_]+) is required', "required"),
                (r'([A-Za-z_]+) invalid', "invalid"),
            ]

            for pattern, ctype in patterns:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    param = m.group(1)

                    # ⭐ 数值约束
                    if ctype in ["min", "max"]:
                        value = m.group(2)
                        return param, f"{ctype}={value}", m.group(0)

                    return param, ctype, m.group(0)

            # ⭐ fallback（保证不会空）
            return None, None, text[:100]

        except Exception as e:
            print("[Collector extract error]", e)
            return None, None, ""

    # ⭐ 主采集函数
    def collect(self, rendered_data, response):
        try:
            print("[Collector] called")
            if not response:
                print("[Collector] response is None")
                return

            method, endpoint, params = self._parse_request(rendered_data)
            param, constraint, error = self._extract_semantics(response)
            if getattr(response, "status_code", None) is not None and int(getattr(response, "status_code", None)) < 400: return
            log_entry = {
                "endpoint": endpoint,
                "method": method,

                "params": params,

                "focus_param": param,
                "focus_value": params.get(param, "") if param else "",

                "constraint": constraint,

                "status": getattr(response, "status_code", None),
                "error": error
            }

            print("[Collector] log:", log_entry)

            with _lock:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry) + "\n")

        except Exception as e:
            print("[Collector Error]", e)


# ⭐ 延迟初始化（防止 import 崩溃）
collector_instance = None


def get_collector():
    global collector_instance
    if collector_instance is None:
        collector_instance = ResponseCollector()
    return collector_instance