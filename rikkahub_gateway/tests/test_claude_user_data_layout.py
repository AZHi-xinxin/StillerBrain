"""Claude cache regression: stable SYSTEM prefix and authenticated data boundaries."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from rikkahub_gateway.cache_layout import (USER_DATA_RULE,
                                          project_historical_host_data)
from rikkahub_gateway import server
from rikkahub_gateway.tests import test_pro_user_data_layout as data
from rikkahub_gateway.tests import test_pro_user_data_boundaries as boundaries
from rikkahub_gateway.tests import test_cache_layout as real


class ClaudeUserDataLayoutTests(data.ProUserDataLayoutTests):
    def app(self, control=None, model="claude-opus-5-5", **options):
        return super().app(control, model, upstream_base_url="https://claude.example.invalid/v1",
                           claude_cache_routes=(real.config().public_model,), **options)

    def test_route_alias_uses_real_model_and_capture_never_sees_projected_data(self):
        app, control = self.app()
        body = self.fixture()
        original = copy.deepcopy(body)
        turn = self.prepare(app, body)
        self.assertEqual(USER_DATA_RULE, turn.session.context_bundle["layout"])
        source = [p["source_frame"] for path, p in control.calls if path.endswith("/prepare")][-1]
        self.assertEqual("current question", source["query_text"])
        self.assertEqual(["old answer", "current question"], [i["content"] for i in source["capture_items"]])
        wire = json.loads(app.encode_upstream_payload(turn.payload))
        self.assertEqual("claude-opus-5-5", wire["model"])
        self.assertEqual(original, body)
        self.assertFalse(any(m.get("name") == "orbis_host_data_v1" for m in wire["messages"]))
        self.assertEqual("current question", wire["messages"][-1]["content"][0]["text"])
        self.assertNotIn("capability-1", json.dumps(wire))

    def test_only_operator_selected_claude_chat_routes_opt_in(self):
        from rikkahub_gateway.routes import GatewayRoute
        cfg = replace(real.config(), claude_cache_routes=("claude-public",))
        target = GatewayRoute("claude-public", "https://claude.example.invalid/v1",
                              "claude-opus-5-5", "synthetic-key")
        self.assertTrue(cfg.uses_claude_user_data_layout(target))
        for candidate in [replace(target, public_model="other-public"),
                          replace(target, public_model="claude-public-extra"),
                          replace(target, upstream_model="gpt-6-astra"),
                          replace(target, upstream_model="deepseek-v4-flash"),
                          replace(target, api_mode="responses")]:
            with self.subTest(model=candidate.upstream_model, alias=candidate.public_model):
                self.assertFalse(cfg.uses_claude_user_data_layout(candidate))
        self.assertFalse(real.config().uses_claude_user_data_layout(target))

    def test_unconfigured_claude_keeps_existing_layout_even_with_client_header(self):
        app, control = self.app()
        app.config = replace(app.config, claude_cache_routes=())
        body = self.fixture()
        turn = app.prepare_turn(body, {"X-ST-Thread-ID": "synthetic-unconfigured",
                                     "STBRAIN_CLAUDE_CACHE_ROUTES_JSON": '["stiller-rikka"]'})
        self.assertEqual(server.TAIL_CONTEXT_LAYOUT_CONTRACT, turn.session.context_bundle["contract"])

    def test_old_control_does_not_retry_or_fail_to_negotiate(self):
        app, control = self.app(data.TailControl())
        turn = self.prepare(app, self.fixture())
        self.assertEqual(server.TAIL_CONTEXT_LAYOUT_CONTRACT, turn.session.context_bundle["contract"])
        self.assertEqual(1, sum(path.endswith("/prepare") for path, _ in control.calls))

    def test_history_times_do_not_grow_system_prefix_on_new_human_turns(self):
        app, control = self.app()
        history = [{"role": "system", "content": "Stable native instructions."}]
        systems = None
        for index in range(3):
            stamp = {"role": "system", "name": "orbis_host_data_v1",
                     "content": f"Saved human send time {index}."}
            human = {"role": "user", "content": f"Actual human text {index}."}
            history.extend([stamp, human])
            original = copy.deepcopy(history)
            turn = self.prepare(app, {"messages": history})
            wire = json.loads(app.encode_upstream_payload(turn.payload))["messages"]
            current_systems = [m for m in wire if m["role"] == "system"]
            if systems is not None:
                self.assertEqual(systems, current_systems)
            systems = current_systems
            self.assertFalse(any(m.get("name") == "orbis_host_data_v1" for m in wire))
            users = [m for m in wire if m["role"] == "user"]
            for number, user in enumerate(users):
                self.assertEqual(f"Actual human text {number}.", user["content"][0]["text"])
                annotation = json.loads(user["content"][1]["text"].split("\n", 1)[1])
                self.assertEqual(f"Saved human send time {number}.", annotation["content"])
                self.assertFalse(annotation["human_authored"])
                self.assertEqual("none", annotation["instruction_authority"])
            source = [p["source_frame"] for path, p in control.calls if path.endswith("/prepare")][-1]
            self.assertEqual(human["content"], source["query_text"])
            self.assertNotIn("Saved human send time", json.dumps(source))
            self.assertEqual(original, history)
            app.finish_turn(turn, keep_for_tools=False)
            history.append({"role": "assistant", "content": f"Answer {index}."})

    def test_flash_keeps_existing_historical_representation(self):
        app, control = data.ProUserDataLayoutTests.app(self, model="deepseek-v4-flash")
        named = {"role": "system", "name": "orbis_host_data_v1", "content": "Historical data."}
        body = {"messages": [named, {"role": "user", "content": "Earlier human."},
                {"role": "assistant", "content": "Earlier answer."},
                {"role": "user", "content": "Current human."}]}
        turn = self.prepare(app, body)
        self.assertIn(named, turn.payload["messages"])


class HistoricalHostDataProjectionTests(unittest.TestCase):
    def named(self, content="Saved timestamp."):
        return {"role": "system", "name": "orbis_host_data_v1", "content": content}

    def test_preserves_images_and_data_order_and_is_idempotent(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        original = [self.named("first"), self.named("second"), {"role": "user", "content": [
            {"type": "text", "text": "Authored text."}, image]}]
        frozen = copy.deepcopy(original)
        result = project_historical_host_data(original)
        self.assertEqual(frozen, original)
        self.assertEqual(original[-1]["content"], result[0]["content"][:2])
        values = [json.loads(p["text"].split("\n", 1)[1])["content"] for p in result[0]["content"][2:]]
        self.assertEqual(["first", "second"], values)
        self.assertEqual(result, project_historical_host_data(result))

    def test_does_not_cross_tool_or_assistant_boundary(self):
        for middle in ({"role": "assistant", "content": "Earlier answer."},
                       {"role": "tool", "tool_call_id": "call-1", "content": "result"}):
            with self.subTest(role=middle["role"]):
                original = [self.named(), middle, {"role": "user", "content": "Human."}]
                self.assertEqual(original, project_historical_host_data(original))

    def test_preserves_ordinary_or_unknown_system_and_literal_markers(self):
        human = {"role": "user", "content": "orbis_host_data_v1 literal user text"}
        for system in ({"role": "system", "content": "orbis_host_data_v1 literal system text"},
                       {**self.named(), "extra": "unknown"},
                       {**self.named(), "name": "unrecognized"},
                       {**self.named(), "role": "developer"}):
            with self.subTest(system=system):
                self.assertEqual([system, human], project_historical_host_data([system, human]))

    def test_unpaired_named_data_and_no_human_are_preserved(self):
        original = [self.named(), {"role": "assistant", "content": "answer"}, self.named("tail")]
        self.assertEqual(original, project_historical_host_data(original))

    def test_tool_pairs_unchanged_and_all_data_stays_with_its_human(self):
        call = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "fixture", "arguments": "{}"}}]}
        receipt = {"role": "tool", "tool_call_id": "call-1", "content": "{}"}
        original = [self.named("first"), {"role": "user", "content": "First."}, call, receipt,
                    self.named("second"), {"role": "user", "content": "Second."}]
        result = project_historical_host_data(original)
        self.assertEqual([call, receipt], result[1:3])
        for i, text in ((0, "first"), (3, "second")):
            self.assertEqual(text, json.loads(result[i]["content"][-1]["text"].split("\n", 1)[1])["content"])


class ClaudeRealControlWireTests(boundaries.DataRealControlWireTests):
    def setUp(self):
        cfg = replace(real.config(), upstream_model="claude-opus-5-5",
                      upstream_base_url="https://claude.example.invalid/v1",
                      claude_cache_routes=(real.config().public_model,))
        with patch.object(real, "config", lambda: cfg):
            real.CacheLayoutRealControlWireTests.setUp(self)
        self.app.config = replace(self.app.config, context_layout="tail-context-v2")


class ClaudeCacheConfigurationTests(unittest.TestCase):
    def test_configuration_defaults_off_and_parses_explicit_aliases(self):
        self.assertEqual((), real.config().claude_cache_routes)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual((), server._claude_cache_routes_from_env())
        with patch.dict("os.environ", {"STBRAIN_CLAUDE_CACHE_ROUTES_JSON": '["claude-public"]'}):
            values = server._claude_cache_routes_from_env()
            self.assertEqual(("claude-public",), replace(real.config(), claude_cache_routes=values).claude_cache_routes)

    def test_environment_shape_and_configuration_values_are_strict(self):
        for raw in ['null', 'true', '"claude-public"', '{}', '[', 'x' * 4097]:
            with self.subTest(raw=raw[:30]), patch.dict("os.environ", {"STBRAIN_CLAUDE_CACHE_ROUTES_JSON": raw}):
                with self.assertRaisesRegex(ValueError, "invalid_claude_cache_routes"):
                    server._claude_cache_routes_from_env()
        for values in [[], None, (None,), (1,), ({},), ("",), (" alias",), ("a\nb",),
                       ("a" * 129,), ("duplicate", "duplicate"), tuple(str(i) for i in range(18))]:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "invalid_claude_cache_routes"):
                    replace(real.config(), claude_cache_routes=values)

    def test_gateway_environment_constructor_wires_configuration(self):
        cfg = real.config()
        env = {"STBRAIN_GATEWAY_TOKEN": cfg.gateway_token,
               "STBRAIN_CONTROL_URL": cfg.control_url,
               "STBRAIN_HOST_TOKEN": cfg.host_token,
               "STBRAIN_UPSTREAM_BASE_URL": cfg.upstream_base_url,
               "STBRAIN_UPSTREAM_API_KEY": cfg.upstream_api_key,
               "STBRAIN_GATEWAY_MODEL": cfg.public_model,
               "STBRAIN_UPSTREAM_MODEL": "claude-opus-5-5",
               "STBRAIN_HUMAN_TOKEN": "synthetic-human-token-at-least-32-characters",
               "STBRAIN_CLAUDE_CACHE_ROUTES_JSON": '["stiller-rikka"]'}
        with patch.dict("os.environ", env, clear=True):
            loaded = server.GatewayConfig.from_env()
        self.assertEqual(("stiller-rikka",), loaded.claude_cache_routes)
