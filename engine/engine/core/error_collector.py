# error_collector.py
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import atexit
import threading
import traceback
from typing import Any, Dict, Optional, Tuple


class ErrorCollector:
    """
    功能：
    1. 从 RESTler 请求/响应中提取有用信息
    2. 仅保留状态码 >= min_status_code 的记录
    3. 对相同错误进行合并
    4. 输出精简后的 JSONL
    """

    METHOD_RE = re.compile(
        r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+([^\s]+)\s+HTTP/\d\.\d",
        re.I
    )
    STATUS_RE = re.compile(r"HTTP/\d\.\d\s+(\d{3})")
    HEADER_SPLIT = re.compile(r"\r?\n\r?\n", re.M)

    def __init__(self,
                 output_dir: str = "./error_logs",
                 file_name: str = "restler_error_summary.jsonl",
                 only_error_status: bool = True,
                 min_status_code: int = 400,
                 max_body_length: int = 20000):
        self.output_dir = output_dir
        self.file_name = file_name
        self.only_error_status = only_error_status
        self.min_status_code = min_status_code
        self.max_body_length = max_body_length

        os.makedirs(self.output_dir, exist_ok=True)
        self.output_path = os.path.join(self.output_dir, self.file_name)

        self.merged_records = {}
        self.lock = threading.Lock()

        atexit.register(self.close)

    def close(self):
        """
        程序结束时一次性写出合并后的结果
        """
        try:
            with self.lock:
                records = list(self.merged_records.values())

            with open(self.output_path, "w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

            print(f"[ErrorCollector] merged error log saved to: {self.output_path}")
            print(f"[ErrorCollector] total merged records: {len(records)}")
        except Exception as e:
            print(f"[ErrorCollector] close error: {e}")
            print(traceback.format_exc())

    def handle_exchange(self,
                        request_data: Any,
                        response_data: Any,
                        source_func: str = "",
                        extra: Optional[Dict[str, Any]] = None) -> None:
        """
        从一次请求/响应交互中提取精简错误信息并合并
        """
        try:
            req_text = self._to_text(request_data)
            resp_text = self._to_text(response_data)

            method, endpoint_full = self._parse_request_line(req_text)
            status_code = self._parse_status_code(resp_text)
            _, req_body = self._split_headers_body(req_text)
            _, resp_body = self._split_headers_body(resp_text)

            if self.only_error_status and (status_code is None or status_code < self.min_status_code):
                return

            error_msg = self._extract_error_message(resp_body, resp_text)
            error_msg_norm = self._normalize_error_message(error_msg)

            norm_endpoint, params = self._extract_endpoint_and_params(
                endpoint_full, method, req_body, error_msg
            )

            merge_key = (
                norm_endpoint or endpoint_full or "",
                method or "",
                status_code or 0,
                error_msg_norm or ""
            )

            with self.lock:
                if merge_key not in self.merged_records:
                    self.merged_records[merge_key] = {
                        "endpoint": norm_endpoint or endpoint_full,
                        "method": method,
                        "params": params,
                        "status": status_code,
                        "error": error_msg_norm,
                        "count": 1
                    }
                else:
                    self.merged_records[merge_key]["count"] += 1

                    old_params = self.merged_records[merge_key].get("params", {})
                    if isinstance(old_params, dict) and isinstance(params, dict):
                        for k, v in params.items():
                            if k not in old_params:
                                old_params[k] = v

        except Exception as e:
            print(f"[ErrorCollector] handle_exchange error: {e}")
            print(traceback.format_exc())

    def _truncate(self, text: Optional[str]) -> str:
        if text is None:
            return ""
        if len(text) <= self.max_body_length:
            return text
        return text[:self.max_body_length] + "\n...[TRUNCATED]..."

    def _to_text(self, obj: Any) -> str:
        """
        尽量把 RESTler 内部对象转成字符串
        """
        if obj is None:
            return ""

        if isinstance(obj, bytes):
            try:
                return obj.decode("utf-8", errors="replace")
            except Exception:
                return repr(obj)

        if isinstance(obj, str):
            return obj

        candidate_attrs = [
            "to_str", "raw_response", "response_str", "message",
            "_str", "data", "text", "body"
        ]

        for attr in candidate_attrs:
            if hasattr(obj, attr):
                try:
                    val = getattr(obj, attr)
                    if callable(val):
                        val = val()
                    if isinstance(val, bytes):
                        return val.decode("utf-8", errors="replace")
                    return str(val)
                except Exception:
                    pass

        try:
            return str(obj)
        except Exception:
            return repr(obj)

    def _parse_request_line(self, request_text: str) -> Tuple[Optional[str], Optional[str]]:
        if not request_text:
            return None, None
        lines = request_text.splitlines()
        first_line = lines[0] if lines else ""
        m = self.METHOD_RE.search(first_line)
        if m:
            return m.group(1).upper(), m.group(2)
        return None, None

    def _parse_status_code(self, response_text: str) -> Optional[int]:
        if not response_text:
            return None

        lines = response_text.splitlines()
        first_line = lines[0] if lines else ""

        m = self.STATUS_RE.search(first_line)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None

        m2 = re.search(r"status(?:_code)?['\":=\s]+(\d{3})", response_text, re.I)
        if m2:
            try:
                return int(m2.group(1))
            except Exception:
                return None

        return None

    def _split_headers_body(self, raw_text: str) -> Tuple[str, str]:
        if not raw_text:
            return "", ""
        parts = self.HEADER_SPLIT.split(raw_text, maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return raw_text, ""

    def _extract_error_message(self, response_body: str, response_text: str) -> str:
        """
        从 JSON / 文本响应中提取核心错误信息
        """
        body = response_body.strip() if response_body else ""
        if not body:
            return ""

        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                for key in [
                    "error", "message", "detail", "title", "description",
                    "error_description", "trace", "errors"
                ]:
                    if key in obj:
                        val = obj[key]
                        if isinstance(val, (dict, list)):
                            return json.dumps(val, ensure_ascii=False)
                        return str(val)
            elif isinstance(obj, list):
                return json.dumps(obj, ensure_ascii=False)
        except Exception:
            pass

        lines = [x.strip() for x in body.splitlines() if x.strip()]
        if lines:
            return lines[0][:2000]

        return body[:2000]

    def _extract_endpoint_and_params(self,
                                     endpoint_full: Optional[str],
                                     method: Optional[str],
                                     req_body: str,
                                     error_msg: str):
        """
        提取：
        - 归一化 endpoint
        - params（query/body/path）
        """
        if not endpoint_full:
            return None, {}

        params = {}

        path_only = endpoint_full
        if "?" in endpoint_full:
            path_only, query_str = endpoint_full.split("?", 1)
            for part in query_str.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = v
                elif part:
                    params[part] = ""

        body_json = self._try_extract_json(req_body)
        if isinstance(body_json, dict):
            for k, v in body_json.items():
                if k not in params:
                    params[k] = v

        norm_path, path_params = self._normalize_path(path_only, error_msg)
        for k, v in path_params.items():
            if k not in params:
                params[k] = v

        return norm_path, params

    def _normalize_path(self, path: str, error_msg: str):
        """
        归一化路径中的数字 ID
        例如：
        /api/blog/posts/4 -> /api/blog/posts/{id}
        """
        if not path:
            return path, {}

        path_params = {}
        parts = path.strip("/").split("/")
        new_parts = []

        for p in parts:
            if p.isdigit():
                param_name = "id"

                m = re.search(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*" + re.escape(p), error_msg or "")
                if m:
                    param_name = m.group(1)

                new_parts.append("{" + param_name + "}")
                path_params[param_name] = p
            else:
                new_parts.append(p)

        return "/" + "/".join(new_parts), path_params

    def _normalize_error_message(self, error_msg: str) -> str:
        """
        错误文本归一化，方便合并相同类型的错误
        """
        if not error_msg:
            return ""

        msg = error_msg.strip()

        msg = re.sub(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=(\d+)\b", r"\1={\1}", msg)
        msg = re.sub(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
            "{uuid}",
            msg
        )
        msg = re.sub(r"\b\d+\b", "{num}", msg)
        msg = re.sub(r"\s+", " ", msg).strip()

        return msg

    def _try_extract_json(self, text: str):
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return {}


_GLOBAL_COLLECTOR = None
_PATCHED = False


def get_collector() -> ErrorCollector:
    global _GLOBAL_COLLECTOR
    if _GLOBAL_COLLECTOR is None:
        output_dir = os.environ.get("RESTLER_ERROR_COLLECTOR_DIR", "./error_logs")
        file_name = os.environ.get("RESTLER_ERROR_COLLECTOR_FILE", "restler_error_summary.jsonl")
        only_error_status = os.environ.get("RESTLER_ERROR_ONLY_4XX_5XX", "1") == "1"
        min_status = int(os.environ.get("RESTLER_ERROR_MIN_STATUS", "400"))

        _GLOBAL_COLLECTOR = ErrorCollector(
            output_dir=output_dir,
            file_name=file_name,
            only_error_status=only_error_status,
            min_status_code=min_status
        )
    return _GLOBAL_COLLECTOR


def _safe_invoke_original(func, *args, **kwargs):
    return func(*args, **kwargs)


def _guess_request_arg(args, kwargs):
    """
    猜测哪个参数是 request 原文
    """
    for k in ["request_data", "data", "rendered_data", "message", "req", "request"]:
        if k in kwargs:
            return kwargs[k]

    for arg in args:
        text = None
        if isinstance(arg, bytes):
            text = arg.decode("utf-8", errors="replace")
        elif isinstance(arg, str):
            text = arg
        else:
            try:
                text = str(arg)
            except Exception:
                text = None

        if text and re.search(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+.+HTTP/\d\.\d", text, re.I):
            return arg

    return args[0] if args else None


def _guess_response_obj(retval):
    """
    尝试从原函数返回值中找响应对象/字符串
    """
    if retval is None:
        return None

    if isinstance(retval, (str, bytes)):
        return retval

    if isinstance(retval, tuple):
        for item in retval:
            try:
                text = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
            except Exception:
                continue
            if "HTTP/" in text or re.search(r"status(?:_code)?['\":=\s]+\d{3}", text, re.I):
                return item

        for item in reversed(retval):
            if not isinstance(item, bool):
                return item

    return retval


def _make_wrapper(func_name, original_func):
    def wrapper(*args, **kwargs):
        start = time.time()
        req_obj = _guess_request_arg(args, kwargs)
        collector = get_collector()

        try:
            retval = _safe_invoke_original(original_func, *args, **kwargs)
        except Exception as e:
            collector.handle_exchange(
                request_data=req_obj,
                response_data=f"LOCAL_EXCEPTION: {type(e).__name__}: {e}",
                source_func=func_name,
                extra={
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "exception": type(e).__name__,
                    "exception_message": str(e)
                }
            )
            raise

        resp_obj = _guess_response_obj(retval)
        collector.handle_exchange(
            request_data=req_obj,
            response_data=resp_obj,
            source_func=func_name,
            extra={
                "elapsed_ms": int((time.time() - start) * 1000)
            }
        )
        return retval

    wrapper.__name__ = getattr(original_func, "__name__", f"wrapped_{func_name}")
    wrapper.__doc__ = getattr(original_func, "__doc__", None)
    return wrapper


def patch_restler_request_utilities() -> bool:
    """
    给 request_utilities 打补丁
    """
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import restler.engine.core.request_utilities as ru
    except Exception:
        try:
            import engine.core.request_utilities as ru
        except Exception as e:
            print(f"[ErrorCollector] 导入 request_utilities 失败: {e}")
            return False

    candidate_names = [
        "send_request_data",
        "send_request_data_with_retries",
        "send_request_data_and_handle_response",
        "send_request_data_and_get_response",
        "_send_request",
        "execute_token_refresh_cmd"
    ]

    patched_count = 0

    for name in candidate_names:
        if hasattr(ru, name):
            original = getattr(ru, name)
            if callable(original):
                try:
                    setattr(ru, name, _make_wrapper(name, original))
                    patched_count += 1
                    print(f"[ErrorCollector] 已 patch: request_utilities.{name}")
                except Exception as e:
                    print(f"[ErrorCollector] patch {name} 失败: {e}")

    try:
        import restler.engine.transport_layer.messaging as messaging
        for name in ["sendRecv", "send", "_sendRequest", "_recvResponse"]:
            if hasattr(messaging, name):
                original = getattr(messaging, name)
                if callable(original):
                    try:
                        setattr(messaging, name, _make_wrapper(f"messaging.{name}", original))
                        patched_count += 1
                        print(f"[ErrorCollector] 已 patch: messaging.{name}")
                    except Exception as e:
                        print(f"[ErrorCollector] patch messaging.{name} 失败: {e}")
    except Exception:
        pass

    _PATCHED = patched_count > 0
    print(f"[ErrorCollector] patch 完成，patched_count={patched_count}")
    return _PATCHED


def enable_error_collector() -> bool:
    ok = patch_restler_request_utilities()
    if ok:
        print("[ErrorCollector] 错误采集模块已启用。")
    else:
        print("[ErrorCollector] 错误采集模块启用失败。")
    return ok