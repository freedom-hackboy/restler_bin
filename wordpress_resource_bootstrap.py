#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


def load_auth_header(settings_path: str) -> str:
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)
    data = (
        settings.get("authentication", {})
        .get("token", {})
        .get("module", {})
        .get("data", {})
    )
    username = str(data.get("username", "")).strip()
    app_password = str(data.get("application_password", "")).strip()
    if not username or not app_password:
        raise ValueError("engine_settings.json must contain WordPress username and application_password")
    token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class WordPressClient:
    def __init__(self, base_url: str, auth_header: str, timeout_sec: int = 20):
        self.base_url = base_url.rstrip("/")
        self.auth_header = auth_header
        self.timeout_sec = timeout_sec

    def post(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._request("POST", path, payload)

    def get(self, path: str) -> Optional[Any]:
        return self._request("GET", path, None)

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]]) -> Optional[Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": self.auth_header,
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                text = response.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="ignore")
            print(f"[Bootstrap] {method} {path} failed: HTTP {exc.code} {text[:300]}")
            return None
        except Exception as exc:
            print(f"[Bootstrap] {method} {path} failed: {exc}")
            return None
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def first_id(value: Optional[Any]) -> Optional[int]:
    if isinstance(value, dict) and isinstance(value.get("id"), int):
        return int(value["id"])
    return None


def add_resource(pool: Dict[str, Any], resource_key: str, resource_id: Optional[int]) -> None:
    if resource_id is None:
        return
    entry = pool.setdefault("resources", {}).setdefault(resource_key, {"ids": [], "sources": []})
    if resource_id not in entry["ids"]:
        entry["ids"].append(resource_id)
    if "bootstrap" not in entry["sources"]:
        entry["sources"].append("bootstrap")


def build_pool(client: WordPressClient) -> Dict[str, Any]:
    suffix = time.strftime("%Y%m%d%H%M%S")
    pool: Dict[str, Any] = {
        "schema_version": 1,
        "resource_count": 0,
        "resources": {},
        "note": "Bootstrap resources created before RESTler parallel replay.",
    }

    post = client.post(
        "/wp-json/wp/v2/posts",
        {
            "title": f"restler bootstrap post {suffix}",
            "content": "restler bootstrap content",
            "status": "publish",
            "comment_status": "open",
            "ping_status": "open",
        },
    )
    post_id = first_id(post)
    add_resource(pool, "/wp-json/wp/v2/posts", post_id)

    comment_host = client.post(
        "/wp-json/wp/v2/posts",
        {
            "title": f"restler bootstrap comment host {suffix}",
            "content": "restler bootstrap comment host content",
            "status": "publish",
            "comment_status": "open",
            "ping_status": "open",
        },
    )
    comment_host_id = first_id(comment_host)
    add_resource(pool, "/wp-json/wp/v2/comment_hosts", comment_host_id)

    page = client.post(
        "/wp-json/wp/v2/pages",
        {
            "title": f"restler bootstrap page {suffix}",
            "content": "restler bootstrap page content",
            "status": "publish",
        },
    )
    add_resource(pool, "/wp-json/wp/v2/pages", first_id(page))

    category = client.post(
        "/wp-json/wp/v2/categories",
        {
            "name": f"restler-bootstrap-category-{suffix}",
            "description": "RESTler bootstrap category",
        },
    )
    add_resource(pool, "/wp-json/wp/v2/categories", first_id(category))

    tag = client.post(
        "/wp-json/wp/v2/tags",
        {
            "name": f"restler-bootstrap-tag-{suffix}",
            "description": "RESTler bootstrap tag",
        },
    )
    add_resource(pool, "/wp-json/wp/v2/tags", first_id(tag))

    user = client.post(
        "/wp-json/wp/v2/users",
        {
            "username": f"restler_user_{suffix}",
            "email": f"restler_user_{suffix}@example.com",
            "password": "StrongPass123!",
            "roles": ["subscriber"],
        },
    )
    add_resource(pool, "/wp-json/wp/v2/users", first_id(user))

    if comment_host_id is not None:
        comment = client.post(
            "/wp-json/wp/v2/comments",
            {
                "post": comment_host_id,
                "content": "restler bootstrap comment",
                "author_name": "restler",
                "author_email": f"restler_comment_{suffix}@example.com",
            },
        )
        add_resource(pool, "/wp-json/wp/v2/comments", first_id(comment))

    pool["resource_count"] = len(pool.get("resources", {}))
    return pool


def write_json(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create stable WordPress resources and write a RESTler resource pool.")
    parser.add_argument("--base_url", default="http://192.168.65.128:8088")
    parser.add_argument("--settings", default="engine_settings.json")
    parser.add_argument("--output", default="rounds/resource_pool.bootstrap.json")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    client = WordPressClient(args.base_url, load_auth_header(args.settings))
    pool = build_pool(client)
    write_json(args.output, pool)
    print(f"[Bootstrap] resource_count={pool['resource_count']} output={args.output}")


if __name__ == "__main__":
    main()
