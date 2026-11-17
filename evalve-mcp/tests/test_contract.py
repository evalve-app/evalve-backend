"""Each path, method and field that the tools use is in the committed OpenAPI contract of the backend."""

import json
import re
import unittest
from pathlib import Path

from helpers import START, FakeBackend, answer, session

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "openapi" / "evalve-backend.json").read_text())
ANY = "any"


def shape(schema, document: dict):
    """The field names, the required names and the value types of a schema, with each `$ref` resolved."""
    if schema in ({}, True):
        return ANY
    if "$ref" in schema:
        target = document
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return shape(target, document)
    if "anyOf" in schema:
        options = [shape(option, document) for option in schema["anyOf"] if option.get("type") != "null"]
        if len(options) == 1:
            return options[0]
        return tuple(sorted(sum((list(option) for option in options), [])))
    if "enum" in schema:
        return {"enum": sorted(schema["enum"])}
    if schema.get("type") == "array":
        return [shape(schema.get("items", {}), document)]
    if schema.get("type") == "object":
        fields = {name: shape(field, document) for name, field in schema.get("properties", {}).items()}
        extra = schema.get("additionalProperties")
        if extra not in (None, False):
            fields["*"] = shape(extra, document)
        return {"fields": fields, "required": sorted(schema.get("required", []))}
    return (schema["type"],)


def without(fields: dict, name: str) -> dict:
    return {
        "fields": {key: value for key, value in fields["fields"].items() if key != name},
        "required": [key for key in fields["required"] if key != name],
    }


def operation(method: str, path: str) -> dict:
    """The contract operation that a request matches. Fails when the contract has no such route."""
    for template, methods in CONTRACT["paths"].items():
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), path) and method.lower() in methods:
            return methods[method.lower()]
    raise AssertionError(f"The contract has no route {method} {path}")


def body_schema(route: dict) -> dict:
    return route["requestBody"]["content"]["application/json"]["schema"]


def parameters(route: dict, where: str) -> dict:
    return {item["name"]: item["schema"] for item in route.get("parameters", []) if item["in"] == where}


class ContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeBackend(
            answer(201), answer(), answer(200, {"id": "account-1", "name": "alyssa"}), answer(), answer(201), answer(), answer(),
        )
        async with session(self.fake) as client:
            self.tools = {tool.name: tool.inputSchema for tool in (await client.list_tools()).tools}
            await client.call_tool("evalve_loop_start", START)
            await client.call_tool("evalve_loop_status", {"loop_id": "loop-1"})
            await client.call_tool("evalve_loop_list", {"status": "running", "cursor": "abc"})
            await client.call_tool(
                "evalve_loop_propose_experiment", {"loop_id": "loop-1", "experiment": 1, "change": "x", "model": "m"},
            )
            await client.call_tool("evalve_loop_cancel", {"loop_id": "loop-1"})
            await client.call_tool("evalve_loop_run_evidence", {"loop_id": "loop-1", "experiment": 0, "run": 1})
        self.routes = [
            (request, operation(request.method, request.url.path))
            for request in self.fake.requests
        ]

    def tool_shape(self, name: str):
        return shape(self.tools[name], self.tools[name])

    def test_each_request_is_a_route_of_the_contract_with_its_parameters(self):
        self.assertEqual(len(self.routes), 7)
        for request, route in self.routes:
            with self.subTest(route=f"{request.method} {request.url.path}"):
                self.assertLessEqual(set(request.url.params), set(parameters(route, "query")))
                for header in parameters(route, "header"):
                    self.assertIn(header.lower(), request.headers)

    def test_the_health_route_is_in_the_contract(self):
        operation("GET", "/evalve/health")

    def test_the_start_parameters_are_the_fields_of_the_start_body(self):
        route = operation("POST", "/evalve/v1/loops")
        self.assertEqual(self.tool_shape("evalve_loop_start"), shape(body_schema(route), CONTRACT))
        self.assertIn("Idempotency-Key", parameters(route, "header"))

    def test_the_propose_parameters_are_the_fields_of_the_propose_body(self):
        route = operation("POST", "/evalve/v1/loops/loop-1/experiments")
        self.assertEqual(
            without(self.tool_shape("evalve_loop_propose_experiment"), "loop_id"), shape(body_schema(route), CONTRACT),
        )

    def test_the_list_parameters_are_query_parameters_of_the_list_route(self):
        query = parameters(operation("GET", "/evalve/v1/loops"), "query")
        tool = self.tool_shape("evalve_loop_list")["fields"]
        self.assertIn("owner_account_id", query)
        for name, field in tool.items():
            self.assertEqual(field, shape(query[name], CONTRACT), name)

    def test_the_path_parameters_have_the_types_of_the_contract(self):
        route = operation("GET", "/evalve/v1/loops/loop-1/experiments/0/runs/1")
        path = {name: shape(schema, CONTRACT) for name, schema in parameters(route, "path").items()}
        self.assertEqual(self.tool_shape("evalve_loop_run_evidence")["fields"], path)
        for name in ("evalve_loop_status", "evalve_loop_cancel"):
            self.assertEqual(self.tool_shape(name), {"fields": {"loop_id": ("string",)}, "required": ["loop_id"]})

    def test_the_answer_fields_that_the_server_reads_are_in_the_contract(self):
        schemas = CONTRACT["components"]["schemas"]
        self.assertIn("detail", schemas["ApiError"]["properties"])
        self.assertIn("id", schemas["Account"]["properties"])
        self.assertIn("next_cursor", schemas["LoopList"]["properties"])
        for field in ("needs", "next", "winner", "outcome"):
            self.assertIn(field, schemas["LoopDetail"]["properties"])
        self.assertIn("idle_deadline", schemas["NeedsChange"]["properties"])


if __name__ == "__main__":
    unittest.main()
