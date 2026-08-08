"""Prompt planner tests."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from kentsugi.catalog import load_catalog
from kentsugi.planner import (
    AgentRouterPlanner,
    AgentRouterResponsesPlanner,
    _json_from_text,
    configured_planner,
    demo_plan,
)
from kentsugi.validate import errors, validate


class DemoPlannerTests(unittest.TestCase):
    def test_product_ad_prompt_is_renderable(self) -> None:
        doc = demo_plan("Create a luxury perfume advertisement at sunset")
        self.assertEqual([], errors(validate(doc, load_catalog())))
        self.assertEqual("orbit", doc["camera"]["type"])
        self.assertEqual("product", doc["camera"]["target"])
        self.assertEqual("studio", doc["environment"]["preset"])
        self.assertEqual("aura_bottle", doc["objects"][0]["asset"])

    def test_can_prompts_select_structural_variants(self) -> None:
        catalog = load_catalog()
        energy = demo_plan("Create a Monster energy can drink advertisement")
        soda = demo_plan("Create a normal soda can advertisement")
        self.assertEqual([], errors(validate(energy, catalog)))
        self.assertEqual([], errors(validate(soda, catalog)))
        self.assertEqual("tall_energy_can", energy["objects"][0]["asset"])
        self.assertEqual("standard_soda_can", soda["objects"][0]["asset"])
        self.assertGreater(catalog.require("tall_energy_can").height, catalog.require("standard_soda_can").height)
        self.assertEqual(
            catalog.require("tall_energy_can").source["recipe"],
            catalog.require("standard_soda_can").source["recipe"],
        )

    def test_character_car_prompt_adds_walk_when_requested(self) -> None:
        doc = demo_plan("A boy walks toward a car in a rainy city at night")
        self.assertEqual([], errors(validate(doc, load_catalog())))
        self.assertEqual("walk_to", doc["actions"][0]["type"])
        self.assertEqual("rain", doc["environment"]["weather"])

    def test_empty_prompt_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            demo_plan("   ")

    def test_model_json_extraction_accepts_fenced_json(self) -> None:
        self.assertEqual({"ok": True}, _json_from_text("```json\n{\"ok\": true}\n```"))

    def test_agentrouter_configuration_uses_environment_without_persisting_secrets(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KENTSUGI_PLANNER": "agentrouter",
                "ANTHROPIC_AUTH_TOKEN": "test-token",
                "ANTHROPIC_BASE_URL": "https://gateway.example",
                "ANTHROPIC_MODEL": "test-model",
            },
            clear=True,
        ):
            name, planner = configured_planner()
            self.assertEqual("agentrouter", name)
            self.assertEqual("test-model", planner.__self__.model)
            self.assertEqual("https://gateway.example", planner.__self__.base_url)

    def test_agentrouter_repairs_one_invalid_document(self) -> None:
        invalid = {"kentsugi_version": "0.1.0"}
        valid = demo_plan("Create a product advertisement")
        planner = AgentRouterPlanner("test-token")
        with patch.object(planner, "_complete", side_effect=[str_json(invalid), str_json(valid)]) as complete:
            result = planner.plan("Create a product advertisement")
        self.assertEqual("product", result["objects"][0]["id"])
        self.assertEqual(2, complete.call_count)

    def test_responses_configuration_is_preferred_in_auto_mode(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KENTSUGI_PLANNER": "auto",
                "AGENTROUTER_API_KEY": "test-token",
                "AGENTROUTER_RESPONSES_BASE_URL": "https://gateway.example/v1",
                "AGENTROUTER_RESPONSES_MODEL": "gpt-5.6-sol",
                "AGENTROUTER_REASONING_EFFORT": "high",
                "ANTHROPIC_AUTH_TOKEN": "anthropic-test-token",
            },
            clear=True,
        ):
            name, planner = configured_planner()
        self.assertEqual("agentrouter-responses", name)
        self.assertEqual("gpt-5.6-sol", planner.__self__.model)
        self.assertEqual("https://gateway.example/v1", planner.__self__.base_url)
        self.assertEqual("high", planner.__self__.reasoning_effort)

    def test_responses_request_uses_responses_wire_shape(self) -> None:
        response = MagicMock()
        response.read.return_value = json_bytes(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"ok":true}'}],
                    }
                ]
            }
        )
        response.__enter__.return_value = response
        planner = AgentRouterResponsesPlanner(
            "test-token", base_url="https://gateway.example/v1", reasoning_effort="medium"
        )
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            text = planner._complete([{"role": "user", "content": "make a scene"}], "instructions")

        self.assertEqual('{"ok":true}', text)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual("https://gateway.example/v1/responses", request.full_url)
        self.assertEqual("gpt-5.6-sol", body["model"])
        self.assertEqual({"effort": "medium"}, body["reasoning"])
        self.assertEqual("json_object", body["text"]["format"]["type"])
        self.assertEqual("Bearer test-token", request.get_header("Authorization"))

    def test_responses_planner_repairs_one_invalid_document(self) -> None:
        invalid = {"kentsugi_version": "0.1.0"}
        valid = demo_plan("Create a product advertisement")
        planner = AgentRouterResponsesPlanner("test-token")
        with patch.object(planner, "_complete", side_effect=[str_json(invalid), str_json(valid)]) as complete:
            result = planner.plan("Create a product advertisement")
        self.assertEqual("product", result["objects"][0]["id"])
        self.assertEqual(2, complete.call_count)


def str_json(value) -> str:
    import json

    return json.dumps(value)


def json_bytes(value) -> bytes:
    return str_json(value).encode("utf-8")

