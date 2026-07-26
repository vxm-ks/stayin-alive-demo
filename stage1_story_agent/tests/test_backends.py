from __future__ import annotations

import json
import os
import unittest

import httpx
from pydantic import SecretStr

from stage1_story_agent.backends import ChatMessage, CompletionRequest, DeepSeekBackend
from stage1_story_agent.agent import Stage1StoryAgent
from stage1_story_agent.config import Stage1Config
from stage1_story_agent.errors import BackendError, ConfigurationError
from stage1_story_agent.tests.fixtures import make_test_mode_request


def config(**overrides) -> Stage1Config:
    values = {"api_key": SecretStr("test-secret"), "max_network_attempts": 3}
    values.update(overrides)
    return Stage1Config(**values)


def request() -> CompletionRequest:
    return CompletionRequest(messages=[ChatMessage("system", "Return json."), ChatMessage("user", "Return json now.")])


class DeepSeekBackendTests(unittest.TestCase):
    def test_success_uses_json_and_drops_reasoning_content(self):
        seen = {}

        def handler(http_request: httpx.Request) -> httpx.Response:
            seen["authorization"] = http_request.headers["Authorization"]
            seen["body"] = json.loads(http_request.content)
            return httpx.Response(200, json={
                "id": "request-1", "model": "deepseek-v4-pro",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}", "reasoning_content": "secret chain"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            })

        client = httpx.Client(transport=httpx.MockTransport(handler))
        response = DeepSeekBackend(config(), client=client).complete(request())
        self.assertEqual(response.content, "{}")
        self.assertFalse(hasattr(response, "reasoning_content"))
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(seen["body"]["thinking"], {"type": "enabled"})
        self.assertEqual(seen["authorization"], "Bearer test-secret")

    def test_retryable_status_then_success_counts_network_attempts(self):
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(500, json={"error": "temporary"})
            return httpx.Response(200, json={"id": "ok", "model": "deepseek-v4-pro", "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        response = DeepSeekBackend(config(), client=client).complete(request())
        self.assertEqual(response.network_attempts, 2)
        self.assertEqual(calls, 2)

    def test_authentication_failure_is_not_retried(self):
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(401, json={"error": "no"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(BackendError) as caught:
            DeepSeekBackend(config(), client=client).complete(request())
        self.assertEqual(caught.exception.code, "DEEPSEEK_AUTH_FAILED")
        self.assertEqual(calls, 1)


def integration_enabled() -> bool:
    if os.getenv("RUN_DEEPSEEK_INTEGRATION") != "1":
        return False
    try:
        Stage1Config.from_env(require_api_key=True)
    except ConfigurationError:
        return False
    return True


@unittest.skipUnless(integration_enabled(), "set RUN_DEEPSEEK_INTEGRATION=1 and configure DEEPSEEK_API_KEY for the explicit real API test")
class DeepSeekIntegrationTests(unittest.TestCase):
    def test_real_json_output(self):
        backend = DeepSeekBackend(Stage1Config.from_env())
        try:
            response = backend.complete(request())
        finally:
            backend.close()
        self.assertIsInstance(json.loads(response.content), dict)

    def test_real_agent_test_mode_keeps_story_controlled_profile(self):
        backend = DeepSeekBackend(Stage1Config.from_env())
        try:
            run = Stage1StoryAgent(backend).plan(make_test_mode_request())
        finally:
            backend.close()

        plan = run.content_plan
        self.assertEqual(plan.form_plan.form_string, "A-B-A")
        self.assertEqual([item.bar_count for item in plan.form_plan.sections], [16, 16, 16])
        self.assertEqual(plan.global_.tempo_bpm, 96)
        self.assertEqual(plan.global_.global_tonality.tonic, "C")
        self.assertEqual(plan.global_.global_tonality.mode, "minor")
        self.assertEqual(len(plan.theme_families), 2)
        self.assertTrue(
            all(item.musecoco_attribute_targets.I1s2 for item in plan.theme_families)
        )
        self.assertEqual(run.content_plan.provenance.provider, "deepseek")
        self.assertEqual(run.content_plan.provenance.prompt_version, "stage1-form-v9")


if __name__ == "__main__":
    unittest.main()
