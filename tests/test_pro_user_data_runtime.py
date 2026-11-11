import copy
import unittest
from runtime.onboarding import ModuleOneOnboardingStore, OnboardingError, _sha256
from rikkahub_gateway.cache_layout import USER_DATA_RULE
from tests import test_tail_context_v2_runtime as tail


class ProUserDataRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.base=tail.TailContextV2RuntimeTests(methodName='runTest');self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def test_v3_real_store_hash_binding_confirm_and_cold_reopen(self):
        base=self.base;wake=base.base.issue()
        fields={'context_layout_offer':copy.deepcopy(USER_DATA_RULE)}
        result=base.prepare(wake,fields);bundle=result['context_bundle']
        self.assertEqual(USER_DATA_RULE,bundle['layout'])
        self.assertEqual(_sha256(bundle),result['context_hash'])
        self.assertEqual('injected',base.base.confirm(wake,result['context_hash'])['decision'])
        base.reopen();again=base.prepare(wake,fields)
        self.assertEqual(bundle,again['context_bundle'])
        with self.assertRaises(OnboardingError):
            base.prepare(wake,{'context_layout_offer':{**USER_DATA_RULE,'insertion_rule':'unknown'}})

    def test_control_advertises_capability_only_over_authenticated_endpoint(self):
        import json
        base=self.base;token='synthetic-host-token-'+'h'*32;app=base.control(token)
        body=json.dumps({'host_id':'host:layout','thread_id':'thread:layout',
                         'source_kind':'human_message','source_event_id':'synthetic-v3'}).encode()
        self.assertEqual(401,app.handle('POST','/v1/host/wakes',{'Content-Type':'application/json'},body)[0])
        status,wake=app.handle('POST','/v1/host/wakes',{'Content-Type':'application/json','Authorization':'Bearer '+token},body)
        self.assertEqual(200,status)
        self.assertIn(USER_DATA_RULE['contract'],wake['context_layout_capabilities'])
        status,result=base.post(app,base.control_body(wake,{'context_layout_offer':USER_DATA_RULE}),token)
        self.assertEqual(200,status);self.assertEqual(USER_DATA_RULE,result['context_bundle']['layout'])
