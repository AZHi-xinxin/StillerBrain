"""Server-owned route regressions; all model traffic uses in-process fake HTTP."""
import io
import json
import os
import threading
import unittest
from dataclasses import FrozenInstanceError, replace
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import httpx

from rikkahub_gateway.routes import GatewayRoute, routes_from_env
from rikkahub_gateway.server import GatewayApplication, GatewayConfig, GatewayError, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests.test_gateway_empty_recovery import EMPTY, ANSWER, event, DONE
from rikkahub_gateway.tests.test_gateway_balance import response as balance_response

SECOND_KEY = "synthetic-second-upstream-secret-" + "b" * 32
SECOND = GatewayRoute("second/model", "https://second.invalid/v1", "vendor/pro", SECOND_KEY)
REF = "STBRAIN_UPSTREAM_ROUTE_SECOND_API_KEY"


def record(**changes):
    return {"public_model": SECOND.public_model, "upstream_base_url": SECOND.upstream_base_url,
            "upstream_model": SECOND.upstream_model, "api_key_env": REF, **changes}


def result(text="safe text"):
    return {"model": "provider-internal", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


class RegistryTests(unittest.TestCase):
    def load(self, value, **env):
        return routes_from_env({REF: SECOND_KEY, "STBRAIN_GATEWAY_ROUTES_JSON": value, **env})

    def test_missing_or_empty_registry_preserves_legacy(self):
        self.assertEqual((), routes_from_env({}))
        self.assertEqual((), self.load("[]"))

    def test_existing_unicode_legacy_labels_remain_verbatim_but_control_characters_fail(self):
        legacy_config = replace(config(), public_model="阿止 自用模型", upstream_model="原有上游模型")
        self.assertEqual("阿止 自用模型", legacy_config.public_model)
        self.assertEqual("原有上游模型", legacy_config.upstream_model)
        for value in ["bad\rmodel", "bad\x7fmodel"]:
            with self.assertRaises(ValueError):
                replace(config(), public_model=value)
        with self.assertRaises(ValueError):
            self.load(json.dumps([record(public_model="新模型")]))

    def test_exact_refs_pretty_json_and_reused_account_key(self):
        loaded = self.load(json.dumps([record(), record(public_model="second/flash")], indent=2))
        self.assertEqual(SECOND, loaded[0])
        self.assertEqual(loaded[0].api_key, loaded[1].api_key)
        with self.assertRaises(FrozenInstanceError):
            loaded[0].api_key = "changed"
        self.assertNotIn(SECOND_KEY, repr(loaded))

    def test_bad_json_shape_duplicate_keys_and_deep_json_are_sanitized(self):
        cases = ["", "null", "{}", "1", '"text"', '[{"public_model":"a","public_model":"b"}]',
                 "[" * 2000 + "]" * 2000, json.dumps([record()] * 17), " " * 65537]
        for raw in cases:
            with self.subTest(raw=raw[:30]), self.assertRaisesRegex(ValueError, "^invalid_gateway_routes$"):
                self.load(raw)

    def test_exact_field_shape_no_inline_key_or_ambient_credential_ref(self):
        for value in [record(api_key=SECOND_KEY), record(extra="x"), record(api_key_env="STBRAIN_HOST_TOKEN"),
                      record(api_key_env="STBRAIN_UPSTREAM_ROUTE_lower_API_KEY"), record(api_key_env="PATH"),
                      record(public_model=123), {key: val for key, val in record().items() if key != "api_key_env"}]:
            with self.subTest(keys=list(value)), self.assertRaisesRegex(ValueError, "^invalid_gateway_routes$"):
                self.load(json.dumps([value]))

    def test_missing_or_invalid_secret_fails_without_echo(self):
        for key in ["", "secret\r\nHeader: value", " a", "密钥", "x" * 4097]:
            with self.subTest(length=len(key)), self.assertRaisesRegex(ValueError, "^invalid_gateway_routes$"):
                self.load(json.dumps([record()]), **{REF: key})
        with self.assertRaisesRegex(ValueError, "^invalid_gateway_routes$"):
            routes_from_env({"STBRAIN_GATEWAY_ROUTES_JSON": json.dumps([record()])})

    def test_visible_core_credentials_cannot_be_reused_as_route_keys(self):
        for name in ["STBRAIN_GATEWAY_TOKEN", "STBRAIN_HOST_TOKEN", "STBRAIN_HUMAN_TOKEN", "STBRAIN_MCP_TOKEN", "STBRAIN_WAKE_SECRET"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load(json.dumps([record()]), **{name: SECOND_KEY})

    def test_unsafe_addresses_fail_before_any_network(self):
        for url in ["http://public.invalid", "https://user:pass@host.invalid", "https://host.invalid?",
                    "https://host.invalid#", "https://host.invalid?a=b", "https://host.invalid/#frag",
                    "https://host.invalid/a%2fb", "https://host.invalid/../v1", "https://host.invalid/./v1",
                    "https://host.invalid\\v1", "https://host.invalid:0", "https://host.invalid:65536",
                    "https://host.invalid/\n", "https://host.invalid/\x7f", "file:///tmp", "https://"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(json.dumps([record(upstream_base_url=url)]))

    def test_https_and_literal_loopback_http_only_for_additional_routes(self):
        for url in ["https://provider.invalid/v1", "https://provider.invalid:8443/prefix/v1/",
                    "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1", "http://localhost/v1"]:
            with self.subTest(url=url):
                self.assertEqual(url, self.load(json.dumps([record(upstream_base_url=url)]))[0].upstream_base_url)

    def test_duplicate_default_or_aux_aliases_and_authority_collisions_rejected(self):
        for route in [replace(SECOND, public_model=config().public_model),
                      replace(SECOND, public_model=SECOND.auxiliary_model),
                      replace(SECOND, api_key=config().gateway_token),
                      replace(SECOND, api_key=config().host_token)]:
            with self.subTest(model=route.public_model), self.assertRaises(ValueError):
                replace(config(), extra_routes=(route,))
        with self.assertRaises(ValueError):
            replace(config(), extra_routes=(SECOND, SECOND))

    def test_credentials_never_become_public_or_logged_model_labels(self):
        for route in [replace(SECOND, public_model=SECOND_KEY), replace(SECOND, upstream_model=SECOND_KEY),
                      replace(SECOND, upstream_base_url="https://provider.invalid/" + SECOND_KEY)]:
            with self.assertRaises(ValueError):
                replace(config(), extra_routes=(route,))
        value = replace(config(), extra_routes=(SECOND,))
        for secret in [value.gateway_token, value.host_token, value.upstream_api_key, SECOND_KEY]:
            self.assertNotIn(secret, repr(value))

    def test_registry_fields_cannot_echo_other_route_or_visible_authority_secrets(self):
        other_ref = "STBRAIN_UPSTREAM_ROUTE_OTHER_API_KEY"
        other_key = "synthetic-other-credential-" + "c" * 32
        for field in ["public_model", "upstream_model", "upstream_base_url"]:
            value = "https://provider.invalid/" + other_key if field == "upstream_base_url" else other_key
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(json.dumps([record(**{field: value}), record(public_model="other", api_key_env=other_ref)]),
                          **{other_ref: other_key})
        with self.assertRaises(ValueError):
            self.load(json.dumps([record()]), STBRAIN_MCP_TOKEN=REF)
        with self.assertRaises(ValueError):
            self.load(json.dumps([record(public_model="prefix-legacy-key")]), STBRAIN_UPSTREAM_API_KEY="legacy-key")

    def test_from_env_preserves_legacy_fields_and_loads_only_declared_refs(self):
        env = {"STBRAIN_GATEWAY_TOKEN": config().gateway_token, "STBRAIN_CONTROL_URL": config().control_url,
               "STBRAIN_HOST_TOKEN": config().host_token, "STBRAIN_HUMAN_TOKEN": "human-" + "h" * 40,
               "STBRAIN_UPSTREAM_BASE_URL": config().upstream_base_url,
               "STBRAIN_UPSTREAM_API_KEY": config().upstream_api_key,
               "STBRAIN_GATEWAY_MODEL": config().public_model, "STBRAIN_UPSTREAM_MODEL": config().upstream_model,
               "STBRAIN_GATEWAY_ROUTES_JSON": json.dumps([record()]), REF: SECOND_KEY,
               "STBRAIN_UPSTREAM_ROUTE_UNUSED_API_KEY": "must-not-be-read"}
        with patch.dict(os.environ, env, clear=True):
            actual = GatewayConfig.from_env()
        self.assertEqual((SECOND,), actual.extra_routes)
        self.assertEqual(config().upstream_model, actual.upstream_model)


class ManualRouteTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.requests = []
        self.replies = []
        def respond(request):
            self.requests.append(request)
            if self.replies:
                value = self.replies.pop(0)
                return value(request) if callable(value) else value
            return httpx.Response(200, json=result())
        self.client = httpx.Client(transport=httpx.MockTransport(respond))
        self.app = GatewayApplication(replace(config(), extra_routes=(SECOND,)), control=self.control, upstream=self.client)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.close_short_term)

    def payload(self, model=SECOND.public_model, **extra):
        return {"model": model, "messages": [{"role": "user", "content": "synthetic question"}], **extra}

    def handler(self, payload=None, path="/v1/chat/completions"):
        value = self.payload() if payload is None else payload
        raw = json.dumps(value).encode()
        h = object.__new__(_GatewayHandler)
        h.server = SimpleNamespace(application=self.app)
        h.headers = Message()
        h.headers["Authorization"] = "Bearer " + config().gateway_token
        h.headers["Content-Type"] = "application/json"
        if path.endswith("completions"):
            h.headers["Content-Length"] = str(len(raw))
        h.path = path; h.rfile = io.BytesIO(raw); h.wfile = io.BytesIO()
        h.statuses = []; h.response_headers = {}
        h.send_response = h.statuses.append
        h.send_header = lambda key, value: h.response_headers.update({key: value})
        h.end_headers = lambda: None
        h.close_connection = False
        return h

    def test_directory_lists_registered_public_and_aux_aliases_without_internals(self):
        data = self.app.models()
        self.assertEqual([config().public_model, SECOND.public_model, self.app.auxiliary_model, SECOND.auxiliary_model],
                         [row["id"] for row in data["data"]])
        encoded = json.dumps(data)
        for hidden in [SECOND_KEY, SECOND.upstream_base_url, SECOND.upstream_model]:
            self.assertNotIn(hidden, encoded)
        with self.assertRaises(TypeError):
            self.app.routes["injected"] = SECOND

    def test_unknown_invalid_or_removed_models_have_no_control_or_upstream_effect(self):
        for model in ["unknown", "second/model--bad", None, 42, {}, []]:
            h = self.handler(self.payload(model))
            h.do_POST()
            self.assertEqual([400], h.statuses)
            self.assertEqual("unknown_model", json.loads(h.wfile.getvalue())["error"]["code"])
        self.assertEqual([], self.control.calls)
        self.assertEqual([], self.requests)
        self.assertIsNone(self.app._current_session)

    def test_missing_model_keeps_default_route_for_old_clients(self):
        h = self.handler({"messages": [{"role": "user", "content": "old client"}]})
        h.do_POST()
        self.assertEqual([200], h.statuses)
        self.assertEqual(config().upstream_base_url + "/chat/completions", str(self.requests[0].url))
        self.assertEqual(config().public_model, json.loads(h.wfile.getvalue())["model"])

    def test_json_selected_route_uses_own_url_key_model_and_public_response_id(self):
        h = self.handler()
        h.do_POST()
        self.assertEqual([200], h.statuses)
        request, = self.requests
        self.assertEqual(SECOND.completion_url, str(request.url))
        self.assertEqual("Bearer " + SECOND_KEY, request.headers["Authorization"])
        self.assertEqual(SECOND.upstream_model, json.loads(request.content)["model"])
        self.assertEqual(SECOND.public_model, json.loads(h.wfile.getvalue())["model"])
        self.assertEqual(config().upstream_api_key, self.app.config.upstream_api_key)
        self.assertEqual(1, sum(path == "/v1/host/wakes" for path, _ in self.control.calls))

    def test_switch_after_complete_uses_next_selected_route_without_changing_authority(self):
        self.handler().do_POST()
        self.handler(self.payload(config().public_model)).do_POST()
        self.assertEqual([SECOND_KEY, config().upstream_api_key],
                         [r.headers["Authorization"][7:] for r in self.requests])
        self.assertEqual(2, sum(path == "/v1/host/wakes" for path, _ in self.control.calls))

    def test_continuation_switch_rejected_before_receipts_or_wait_are_consumed(self):
        payload = self.payload(tools=legacy.GatewayTests.bound_tools())
        turn = self.app.prepare_turn(payload, {})
        call = legacy.GatewayTests.native_call("route-call", "synthetic")
        bindings = self.app.bind_response_tool_calls(turn, [call])
        self.app.finish_turn(turn, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        follow = {**payload, "messages": [*payload["messages"],
            {"role": "assistant", "tool_calls": [legacy.GatewayTests.wire_call(call)]},
            {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"exitCode":0}'}]}
        before = list(self.control.calls)
        with self.assertRaises(GatewayError) as failure:
            self.app.prepare_turn({**follow, "model": config().public_model}, {})
        self.assertEqual("tool_continuation_model_mismatch", failure.exception.code)
        self.assertEqual(before, self.control.calls)
        self.assertEqual({call.tool_call_id}, turn.session.expected_tool_call_ids)
        self.assertEqual([], turn.session.host_receipts)
        resumed = self.app.prepare_turn(follow, {})
        self.assertIs(turn.session, resumed.session)
        self.assertEqual(SECOND, resumed.route)
        self.assertEqual(SECOND.upstream_model, resumed.payload["model"])
        self.app.finish_turn(resumed, keep_for_tools=False)

    def test_parallel_normal_conversations_keep_existing_single_wake_fence(self):
        running = self.app.prepare_turn(self.payload(), {"X-Session-ID": "one"})
        with self.assertRaises(GatewayError) as failure:
            self.app.prepare_turn(self.payload(config().public_model), {"X-Session-ID": "two"})
        self.assertEqual("human_turn_in_progress", failure.exception.code)
        self.assertIs(running.session, self.app._current_session)
        self.assertEqual(SECOND, running.route)
        self.app.finish_turn(running, keep_for_tools=False)

    def test_sse_and_reasoning_only_recovery_stay_on_same_route_and_encoded_request(self):
        self.replies = [httpx.Response(200, content=EMPTY), httpx.Response(200, content=ANSWER)]
        h = self.handler(self.payload(stream=True)); h.do_POST()
        self.assertEqual([200], h.statuses)
        self.assertEqual(2, len(self.requests))
        self.assertEqual(self.requests[0].content, self.requests[1].content)
        for request in self.requests:
            self.assertEqual(SECOND.completion_url, str(request.url))
            self.assertEqual("Bearer " + SECOND_KEY, request.headers["Authorization"])
        events = [json.loads(line[6:]) for line in h.wfile.getvalue().splitlines() if line.startswith(b"data: {")]
        self.assertTrue(events)
        self.assertTrue(all(row["model"] == SECOND.public_model for row in events if "error" not in row))
        self.assertEqual(1, sum(path == "/v1/host/wakes" for path, _ in self.control.calls))

    def test_same_prepared_route_survives_unrelated_app_config_replacement(self):
        turn = self.app.prepare_turn(self.payload(), {})
        self.app.config = replace(config(), upstream_model="changed", upstream_base_url="http://changed.test/v1")
        h = self.handler(); h._proxy_json(turn)
        self.assertEqual(SECOND.completion_url, str(self.requests[0].url))
        self.assertEqual(SECOND.public_model, json.loads(h.wfile.getvalue())["model"])

    def test_aux_alias_keeps_own_route_and_no_memory_for_json_and_sse(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.replies = [httpx.Response(200, content=ANSWER)] if stream else []
                h = self.handler(self.payload(SECOND.auxiliary_model, stream=stream)); h.do_POST()
                self.assertEqual([200], h.statuses)
                self.assertEqual([], self.control.calls)
                self.assertEqual(SECOND.completion_url, str(self.requests[-1].url))
                raw = h.wfile.getvalue()
                values = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: {")] if stream else [json.loads(raw)]
                self.assertTrue(all(value["model"] == SECOND.auxiliary_model for value in values))
                self.assertIsNone(self.app._current_session)

    def test_concurrent_aux_requests_do_not_mutate_main_selected_route(self):
        reached, release = threading.Event(), threading.Event()
        errors = []
        def wait(request):
            reached.set()
            if not release.wait(3):
                raise RuntimeError("fixture timeout")
            return httpx.Response(200, json=result())
        self.replies = [wait]
        def aux():
            try:
                self.app.auxiliary_completion(self.payload(SECOND.auxiliary_model))
            except Exception as exc:
                errors.append(type(exc).__name__)
        worker = threading.Thread(target=aux, daemon=True); worker.start()
        try:
            self.assertTrue(reached.wait(2))
            main = self.app.prepare_turn(self.payload(config().public_model), {})
            self.assertEqual(config().upstream_api_key, main.route.api_key)
            self.assertEqual(SECOND_KEY, self.requests[0].headers["Authorization"][7:])
            self.app.finish_turn(main, keep_for_tools=False)
        finally:
            release.set(); worker.join(4)
        self.assertFalse(worker.is_alive()); self.assertEqual([], errors)

    def test_main_json_error_and_sse_block_other_route_keys(self):
        for status, stream in [(200, False), (429, False), (200, True), (429, True)]:
            with self.subTest(status=status, stream=stream):
                body = event({"content": SECOND_KEY}, "stop") + DONE if stream and status == 200 else json.dumps(result(SECOND_KEY)).encode()
                self.replies = [httpx.Response(status, content=body)]
                h = self.handler(self.payload(config().public_model, stream=stream)); h.do_POST()
                raw = h.wfile.getvalue()
                self.assertNotIn(SECOND_KEY.encode(), raw)
                self.assertIn(b"upstream_protected_value", raw)
                self.assertIsNone(self.app._current_session)

    def test_aux_blocks_nonselected_route_key(self):
        self.replies = [httpx.Response(200, json=result(SECOND_KEY))]
        h = self.handler(self.payload(self.app.auxiliary_model)); h.do_POST()
        self.assertEqual([502], h.statuses)
        self.assertNotIn(SECOND_KEY.encode(), h.wfile.getvalue())
        self.assertEqual([], self.control.calls)

    def test_nonselected_secret_split_across_sse_fields_never_reaches_client(self):
        body = event({"reasoning_content": SECOND_KEY[:24]}) + event({"content": SECOND_KEY[24:]}, "stop") + DONE
        self.replies = [httpx.Response(200, content=body)]
        h = self.handler(self.payload(config().public_model, stream=True)); h.do_POST()
        self.assertNotIn(SECOND_KEY[:24].encode(), h.wfile.getvalue())
        self.assertIn(b"upstream_protected_value", h.wfile.getvalue())
        self.assertIsNone(self.app._current_session)

    def test_client_supplied_route_fields_cannot_replace_server_destination_or_credentials(self):
        h = self.handler(self.payload(upstream_base_url="https://attacker.invalid", upstream_api_key="attacker-key"))
        h.do_POST()
        self.assertEqual(SECOND.completion_url, str(self.requests[0].url))
        self.assertEqual("Bearer " + SECOND_KEY, self.requests[0].headers["Authorization"])

    def test_transport_exception_never_echoes_keys_in_response_or_logs(self):
        def fail(request):
            raise httpx.ConnectError("private " + SECOND_KEY, request=request)
        self.replies = [fail]
        h = self.handler()
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as failure, patch("rikkahub_gateway.server._PERFORMANCE_LOGGER.info") as performance:
            h.do_POST()
        self.assertEqual([502], h.statuses)
        self.assertNotIn(SECOND_KEY.encode(), h.wfile.getvalue())
        self.assertNotIn(SECOND_KEY, str(failure.call_args_list) + str(performance.call_args_list))

    def test_balance_legacy_default_and_explicit_registered_route_choose_own_key(self):
        official = replace(SECOND, upstream_base_url="https://api.deepseek.com/v1")
        seen = []
        def answer(request):
            seen.append(request); return balance_response()
        self.app = GatewayApplication(replace(config(), upstream_base_url="https://api.deepseek.com/v1",
                                            extra_routes=(official,)), control=self.control, upstream=self.client,
                                      balance_transport=httpx.MockTransport(answer))
        for path in ["/v1/user/balance", "/v1/models/" + quote(SECOND.public_model, safe="") + "/balance"]:
            h = self.handler(path=path); h.do_GET(); self.assertEqual([200], h.statuses)
        self.assertEqual([config().upstream_api_key, SECOND_KEY], [r.headers["Authorization"][7:] for r in seen])
        self.assertEqual([], self.control.calls); self.assertEqual([], self.requests)

    def test_balance_unknown_aux_query_or_nonofficial_route_never_falls_back(self):
        for path, status, code in [
            ("/v1/models/unknown/balance", 400, "unknown_model"),
            ("/v1/models/" + quote(SECOND.auxiliary_model, safe="") + "/balance", 400, "unknown_model"),
            ("/v1/models/" + quote(SECOND.public_model, safe="") + "/balance", 503, "balance_official_upstream_required"),
            ("/v1/user/balance?model=second", 400, "invalid_balance_request")]:
            h = self.handler(path=path); h.do_GET()
            self.assertEqual([status], h.statuses)
            self.assertEqual(code, json.loads(h.wfile.getvalue())["error"]["code"])
        self.assertEqual([], self.requests); self.assertEqual([], self.control.calls)


if __name__ == "__main__":
    unittest.main()
