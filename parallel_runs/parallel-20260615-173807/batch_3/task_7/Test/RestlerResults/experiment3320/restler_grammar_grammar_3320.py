""" THIS IS AN AUTOMATICALLY GENERATED FILE!"""
from __future__ import print_function
import json
from engine import primitives
from engine.core import requests
from engine.errors import ResponseParsingException
from engine import dependencies
req_collection = requests.RequestCollection([])
# Endpoint: /wp-json/wp/v2/search, method: Get
request = requests.Request([
    primitives.restler_static_string("GET "),
    primitives.restler_basepath(""),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp-json"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("v2"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("search"),
    primitives.restler_static_string("?"),
    primitives.restler_static_string("type="),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False, examples=["post"]),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("search="),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False, examples=["example"]),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("_fields="),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False, examples=["id,slug,title,excerpt,featured_media"]),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("context="),
    primitives.restler_fuzzable_group("context", ['view','embed','edit'] , default_enum="view" ,quoted=False),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("_embed="),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False, examples=["author,wp:term"]),
    primitives.restler_static_string(" HTTP/1.1\r\n"),
    primitives.restler_static_string("Accept: application/json\r\n"),
    primitives.restler_static_string("Host: 192.168.65.128:8088\r\n"),
    primitives.restler_static_string("Accept-Language: "),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False),
    primitives.restler_static_string("\r\n"),
    primitives.restler_refreshable_authentication_token("authentication_token_tag"),
    primitives.restler_static_string("\r\n"),

],
requestId="/wp-json/wp/v2/search"
)
req_collection.add_request(request)
