""" THIS IS AN AUTOMATICALLY GENERATED FILE!"""
from __future__ import print_function
import json
from engine import primitives
from engine.core import requests
from engine.errors import ResponseParsingException
from engine import dependencies
req_collection = requests.RequestCollection([])
# Endpoint: /wp-json/wp/v2/comments, method: Get
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
    primitives.restler_static_string("comments"),
    primitives.restler_static_string("?"),
    primitives.restler_static_string("context="),
    primitives.restler_fuzzable_group("context", ['view','embed','edit'] , default_enum="view" ,quoted=False),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("search="),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("order="),
    primitives.restler_fuzzable_group("order", ['asc','desc'] , default_enum="desc" ,quoted=False),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("orderby="),
    primitives.restler_custom_payload_query("orderby", quoted=False),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("_embed="),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False, examples=["author,wp:term"]),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("page="),
    primitives.restler_fuzzable_int("1"),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("per_page="),
    primitives.restler_fuzzable_int("1"),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("offset="),
    primitives.restler_fuzzable_int("1", examples=["0"]),
    primitives.restler_static_string("&"),
    primitives.restler_static_string("slug="),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False),
    primitives.restler_static_string(" HTTP/1.1\r\n"),
    primitives.restler_static_string("Accept: application/json\r\n"),
    primitives.restler_static_string("Host: 192.168.65.128:8088\r\n"),
    primitives.restler_static_string("Accept-Language: "),
    primitives.restler_fuzzable_string("fuzzstring", quoted=False),
    primitives.restler_static_string("\r\n"),
    primitives.restler_refreshable_authentication_token("authentication_token_tag"),
    primitives.restler_static_string("\r\n"),

],
requestId="/wp-json/wp/v2/comments"
)
req_collection.add_request(request)

# Endpoint: /wp-json/wp/v2/comments, method: Post
request = requests.Request([
    primitives.restler_static_string("POST "),
    primitives.restler_basepath(""),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp-json"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("v2"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("comments"),
    primitives.restler_static_string(" HTTP/1.1\r\n"),
    primitives.restler_static_string("Accept: application/json\r\n"),
    primitives.restler_static_string("Host: 192.168.65.128:8088\r\n"),
    primitives.restler_static_string("Content-Type: "),
    primitives.restler_static_string("application/json"),
    primitives.restler_static_string("\r\n"),
    primitives.restler_refreshable_authentication_token("authentication_token_tag"),
    primitives.restler_static_string("\r\n"),
    primitives.restler_static_string("{"),
    primitives.restler_static_string("""
    "title":"""),
    primitives.restler_fuzzable_string("fuzzstring", quoted=True),
    primitives.restler_static_string(""",
    "content":"""),
    primitives.restler_fuzzable_string("fuzzstring", quoted=True),
    primitives.restler_static_string(""",
    "excerpt":"""),
    primitives.restler_fuzzable_string("fuzzstring", quoted=True),
    primitives.restler_static_string(""",
    "status":"""),
    primitives.restler_custom_payload("status", quoted=True),
    primitives.restler_static_string(""",
    "post":"""),
    primitives.restler_custom_payload("post", quoted=True),
    primitives.restler_static_string("}"),
    primitives.restler_static_string("\r\n"),

],
requestId="/wp-json/wp/v2/comments"
)
req_collection.add_request(request)

# Endpoint: /wp-json/wp/v2/comments/{commentId}, method: Get
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
    primitives.restler_static_string("comments"),
    primitives.restler_static_string("/"),
    primitives.restler_custom_payload("commentId", quoted=False),
    primitives.restler_static_string("?"),
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
requestId="/wp-json/wp/v2/comments/{commentId}"
)
req_collection.add_request(request)

# Endpoint: /wp-json/wp/v2/comments/{commentId}, method: Post
request = requests.Request([
    primitives.restler_static_string("POST "),
    primitives.restler_basepath(""),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp-json"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("wp"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("v2"),
    primitives.restler_static_string("/"),
    primitives.restler_static_string("comments"),
    primitives.restler_static_string("/"),
    primitives.restler_custom_payload("commentId", quoted=False),
    primitives.restler_static_string(" HTTP/1.1\r\n"),
    primitives.restler_static_string("Accept: application/json\r\n"),
    primitives.restler_static_string("Host: 192.168.65.128:8088\r\n"),
    primitives.restler_static_string("Content-Type: "),
    primitives.restler_static_string("application/json"),
    primitives.restler_static_string("\r\n"),
    primitives.restler_refreshable_authentication_token("authentication_token_tag"),
    primitives.restler_static_string("\r\n"),
    primitives.restler_static_string("{"),
    primitives.restler_static_string("""
    "title":"""),
    primitives.restler_fuzzable_string("fuzzstring", quoted=True),
    primitives.restler_static_string(""",
    "content":"""),
    primitives.restler_fuzzable_string("fuzzstring", quoted=True),
    primitives.restler_static_string(""",
    "excerpt":"""),
    primitives.restler_fuzzable_string("fuzzstring", quoted=True),
    primitives.restler_static_string("}"),
    primitives.restler_static_string("\r\n"),

],
requestId="/wp-json/wp/v2/comments/{commentId}"
)
req_collection.add_request(request)

# Endpoint: /wp-json/wp/v2/comments/{commentId}, method: Delete
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
    primitives.restler_static_string("comments"),
    primitives.restler_static_string("/"),
    primitives.restler_custom_payload("commentId", quoted=False),
    primitives.restler_static_string("?"),
    primitives.restler_static_string("force="),
    primitives.restler_fuzzable_bool("true"),
    primitives.restler_static_string(" HTTP/1.1\r\n"),
    primitives.restler_static_string("Accept: application/json\r\n"),
    primitives.restler_static_string("Host: 192.168.65.128:8088\r\n"),
    primitives.restler_refreshable_authentication_token("authentication_token_tag"),
    primitives.restler_static_string("\r\n"),

],
requestId="/wp-json/wp/v2/comments/{commentId}"
)
req_collection.add_request(request)

