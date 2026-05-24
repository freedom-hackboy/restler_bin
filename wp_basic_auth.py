#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
from typing import Any, Dict


def _read_value(data: Dict[str, Any], direct_key: str, env_key: str) -> str:
    direct_value = data.get(direct_key)
    if isinstance(direct_value, str) and direct_value.strip():
        return direct_value.strip()

    env_name = data.get(env_key)
    if isinstance(env_name, str) and env_name.strip():
        import os

        env_value = os.environ.get(env_name.strip(), "")
        if env_value.strip():
            return env_value.strip()

    return ""


def acquire_token(data, log):
    """
    RESTler token module entrypoint.

    Expected output format:
    {'app1': {}}
    Authorization: Basic <base64(username:application_password)>
    """
    data = data or {}
    username = _read_value(data, "username", "username_env")
    app_password = _read_value(data, "application_password", "application_password_env")
    header_name = str(data.get("header_name", "Authorization")).strip() or "Authorization"

    if not username:
        raise ValueError(
            "WordPress auth username is missing. "
            "Set data['username'] or the configured environment variable."
        )
    if not app_password:
        raise ValueError(
            "WordPress application password is missing. "
            "Set data['application_password'] or the configured environment variable."
        )

    raw = f"{username}:{app_password}"
    token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    log("WordPress Basic Auth token generated.")

    metadata = {"app1": {}}
    return f"{metadata}\n{header_name}: Basic {token}\n"
