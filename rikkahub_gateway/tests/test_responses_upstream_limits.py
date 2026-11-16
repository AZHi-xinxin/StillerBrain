import json
import unittest
from unittest.mock import patch
import httpx
from rikkahub_gateway import responses_adapter as adapter
from rikkahub_gateway.tests import test_responses_adapter as fixtures

class ResponsesUpstreamLimitsTests(unittest.TestCase):
 def test_nonstream_and_stream_ignore_legacy_sizes_but_bounded_mode_enforces(self):
  body=fixtures.response(text="完整内容" * 100)
  for stream in (False,True):
   for unrestricted in (False,True):
    with self.subTest(stream=stream,unrestricted=unrestricted), patch.object(adapter,"MAX_EVENT_BYTES",128), patch.object(adapter,"MAX_WIRE_BYTES",256):
     wire=fixtures.events(body) if stream else adapter.encode(body)
     raw=httpx.Response(200,headers={"Content-Type":"text/event-stream" if stream else "application/json"},stream=fixtures.Fragmented(wire,1024))
     converted=adapter.ResponsesAdapter({"model":"synthetic","stream":stream,"messages":[]},upstream_limits_only=unrestricted)
     if unrestricted:
      output=b"".join(converted.wrap(raw).iter_bytes())
      if stream:self.assertIn(b"[DONE]",output)
      else:self.assertEqual("完整内容"*100,json.loads(output)["choices"][0]["message"]["content"])
     else:
      with self.assertRaises(adapter.ResponsesProtocolError):list(converted.wrap(raw).iter_bytes())
 def test_encrypted_tool_continuation_has_no_legacy_aggregate_ceiling(self):
  output=fixtures.response(text="完整内容"*100,calls=True)["output"]
  with patch.object(adapter,"MAX_WIRE_BYTES",64):
   state=adapter.ResponsesTurnState();state.remember(output)
   self.assertEqual(1,len(state.batches))
   state.remember(output);self.assertEqual(1,len(state.batches))
   with self.assertRaises(adapter.ResponsesProtocolError):adapter.ResponsesTurnState().remember(output,upstream_limits_only=False)
