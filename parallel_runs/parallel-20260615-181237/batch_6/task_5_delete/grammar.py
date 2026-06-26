""" THIS IS AN AUTOMATICALLY GENERATED FILE!"""
from __future__ import print_function
import json
from engine import primitives
from engine.core import requests
from engine.errors import ResponseParsingException
from engine import dependencies
req_collection = requests.RequestCollection([])
# Endpoint: /wp-json/wp/v2/categories/{categoryId}, method: Delete
request = requests.Request([
    primitives.restler_static_string("DELETE "),
    primitives.restler_basepath(""),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp-json"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("v2"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("categories"),
    primitives.restler_static_string("/"),
    primitives.restler_custom_payload("categoryId", quoted=False),
    primitives.restler_static_string("?"),
    primitives.restler_static_string("force="),
    primitives.restler_custom_payload_query("force", quoted=False),
    primitives.restler_static_string(" HTTP/1.1\r\n"),
    primitives.restler_static_string("Accept: application/json\r\n"),
    primitives.restler_static_string("Host: 192.168.65.128:8088\r\n"),
    primitives.restler_refreshable_authentication_token("authentication_token_tag"),
    primitives.restler_static_string("\r\n"),

],
requestId="/wp-json/wp/v2/categories/{categoryId}"
)
req_collection.add_request(request)

