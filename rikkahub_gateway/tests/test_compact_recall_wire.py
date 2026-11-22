"""Real Control database and gateway projection, with no real upstream traffic."""
from dataclasses import replace
import json
import unittest
from mcp_server.control_server import ControlApplication
from rikkahub_gateway.server import GatewayApplication
from rikkahub_gateway.short_term_wire import FinalBodyCollector, MARKER
from rikkahub_gateway.tests.test_gateway import config, GatewayTests
from rikkahub_gateway.tests.test_gateway_control_integration import DirectControlClient
from tests import test_onboarding as fixtures


class CompactRecallWireTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModuleOneOnboardingTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.seed_tagless_named_learning()
        token = 'synthetic-compact-control-token-longer-than-32'
        control = ControlApplication(self.fixture.store, owner_id=self.fixture.owner,
            model_id=self.fixture.model, host_token=token,
            human_token='synthetic-human-control-token-longer-than-32', human_actor_id='synthetic-human')
        self.app = GatewayApplication(replace(config(), context_layout='tail-context-v2', short_term_enabled=True),
            control=DirectControlClient(control, token))
        self.addCleanup(self.app.upstream.close)
        self.addCleanup(self.app.close_short_term)
        self.sequence = 0

    def turn(self, window):
        self.sequence += 1
        turn = self.app.prepare_turn(GatewayTests.request([
            {'role':'user', 'content':f'第{self.sequence}轮，你还记得我们之前读的侍魔嘛？'}]),
            {'X-ST-Thread-ID':window})
        return turn

    def complete(self, turn, text):
        self.app.finish_turn(turn, keep_for_tools=False)
        collector = FinalBodyCollector()
        collector.feed({'id':str(self.sequence), 'choices':[{'index':0,'finish_reason':'stop',
            'message':{'role':'assistant','content':text}}]}, stream=False)
        self.app.capture_final_body(turn, collector)

    def projection(self, turn):
        result = json.loads(turn.session.context_bundle['dynamic_message']['content'])
        wire = json.dumps(turn.payload, ensure_ascii=False)
        self.assertNotIn('ST_SOURCE_V1', wire)
        self.assertNotIn('同窗口短期记忆', wire)
        self.assertNotIn('model_identity_basis', wire)
        for row in result['记忆']:
            self.assertTrue({'编号','摘要'} <= set(row) <= {'编号','摘要','来源'})
        return result, wire

    def test_same_window_quiet_switch_keeps_original_handover_then_goes_quiet(self):
        first = self.turn('synthetic-A')
        projection, wire = self.projection(first)
        self.assertNotIn('变化', projection)
        self.assertNotIn(MARKER.rstrip(), wire)
        self.complete(first, '上一窗口完整正文，不含推理。')
        same = self.turn('synthetic-A')
        projection, wire = self.projection(same)
        self.assertNotIn('变化', projection)
        self.assertNotIn(MARKER.rstrip(), wire)
        self.complete(same, '即将交接的完整正文。')
        other = self.turn('synthetic-B')
        projection, wire = self.projection(other)
        self.assertIn('本轮已换窗', projection['变化'])
        self.assertIn('模型未改变', projection['变化'])
        self.assertIn(MARKER.rstrip(), wire)
        self.assertIn('即将交接的完整正文。', wire)
        self.complete(other, '新窗口已读到交接。')
        next_turn = self.turn('synthetic-B')
        projection, wire = self.projection(next_turn)
        self.assertNotIn('变化', projection)
        self.assertNotIn(MARKER.rstrip(), wire)
        self.assertNotIn('即将交接的完整正文。', wire)
        self.complete(next_turn, '继续。')
