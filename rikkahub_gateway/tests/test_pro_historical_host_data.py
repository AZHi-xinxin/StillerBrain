"""Pro history annotations must stay data across human turns and tool calls."""
import copy
import json

from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests.test_pro_user_data_layout import ProUserDataLayoutTests


class ProHistoricalHostDataTests(ProUserDataLayoutTests):
    def test_five_human_turns_keep_system_frames_stable_and_capture_authored(self):
        app, control = self.app()
        history = [{"role": "system", "content": "Stable synthetic instructions."}]
        previous_systems = None
        for index in range(5):
            stamp = {"role": "system", "name": "orbis_host_data_v1",
                     "content": f"Saved send time {index}."}
            human = {"role": "user", "content": f"Authored human turn {index}."}
            history.extend([stamp, human])
            original = copy.deepcopy(history)
            turn = self.prepare(app, {"messages": history})
            wire = json.loads(app.encode_upstream_payload(turn.payload))["messages"]
            systems = [m for m in wire if m["role"] == "system"]
            if previous_systems is not None:
                self.assertEqual(previous_systems, systems)
            previous_systems = systems
            self.assertFalse(any(m.get("name") == "orbis_host_data_v1" for m in wire))
            users = [m for m in wire if m["role"] == "user"]
            self.assertEqual(index + 1, len(users))
            for number, user in enumerate(users):
                self.assertEqual(f"Authored human turn {number}.", user["content"][0]["text"])
                annotation = json.loads(user["content"][1]["text"].split("\n", 1)[1])
                self.assertEqual(f"Saved send time {number}.", annotation["content"])
                self.assertFalse(annotation["human_authored"])
                self.assertEqual("none", annotation["instruction_authority"])
            source = [p["source_frame"] for path, p in control.calls if path.endswith("/prepare")][-1]
            self.assertEqual(human["content"], source["query_text"])
            self.assertNotIn("Saved send time", json.dumps(source))
            self.assertEqual(original, history)
            app.finish_turn(turn, keep_for_tools=False)
            history.append({"role": "assistant", "content": f"Answer {index}."})

    def test_historical_image_and_tool_continuation_preserve_original_lineage(self):
        app, control = self.app()
        body = self.fixture()
        stamp = {"role": "system", "name": "orbis_host_data_v1", "content": "Earlier send time."}
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        earlier = {"role": "user", "content": [{"type": "text", "text": "Earlier human."}, image]}
        body["messages"][1:2] = [stamp, earlier]
        body["tools"] = legacy.GatewayTests.bound_tools()
        original = copy.deepcopy(body)
        first = self.prepare(app, body)
        wire = json.loads(app.encode_upstream_payload(first.payload))["messages"]
        self.assertNotIn(stamp, wire)
        projected_earlier = next(m for m in wire if m["role"] == "user")
        self.assertEqual(earlier["content"], projected_earlier["content"][:2])
        call = legacy.GatewayTests.native_call("history-call", "synthetic no execution")
        bindings = app.bind_response_tool_calls(first, [call])
        app.finish_turn(first, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        pair = [{"role": "assistant", "content": None, "tool_calls": [legacy.GatewayTests.wire_call(call)]},
                {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"exitCode":0}'}]
        second = self.prepare(app, {**body, "messages": body["messages"] + pair})
        self.assertTrue(second.continuation)
        self.assertEqual(first.payload["messages"], second.payload["messages"][:-2])
        self.assertEqual(pair, second.payload["messages"][-2:])
        self.assertEqual(1, sum(path.endswith("/prepare") for path, _ in control.calls))
        self.assertEqual(original, body)

    def test_ordinary_historical_system_is_not_moved_or_reinterpreted(self):
        app, _ = self.app()
        system = {"role": "system", "content": "orbis_host_data_v1 is literal text, not a metadata tag."}
        body = self.fixture()
        body["messages"].insert(1, system)
        original = copy.deepcopy(body)
        turn = self.prepare(app, body)
        self.assertIn(system, turn.payload["messages"])
        self.assertEqual(original, body)
