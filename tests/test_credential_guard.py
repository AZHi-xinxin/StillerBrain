"""Synthetic unit tests for the shared, no-I/O credential detector."""
import copy
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from runtime.credential_guard import contains_credential_or_secret as detects


class CredentialGuardTests(unittest.TestCase):
    def assert_detects(self, values):
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(detects(value))

    def assert_allows(self, values):
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(detects(value))

    def test_direct_natural_assignments(self):
        self.assert_detects([
            "password: DEMO", "passwd=1234", "API key: DEMO", "api_key=DEMO",
            "secret: DEMO", "token: 123456", "cookie: session=DEMO",
            "密码：合成值", "口令=1234", "私钥：合成值", "令牌：合成值", "密钥=合成值",
            "token：合成值", "password＝DEMO", "ｔｏｋｅｎ：DEMO", "ＰＡＳＳＷＯＲＤ：1234",
            "password:\n  DEMO", "密码是合成值", "密码为：合成值", "token is DEMO",
            "access_token：DEMO", "private_key=DEMO", "DB_PASSWORD: DEMO",
        ])

    def test_finite_change_grammar(self):
        self.assert_detects([
            "站点密码轮换：OLD → NEW", "站点密码已轮换为NEW", "密钥更新到：NEW",
            "口令更换成NEW", "密码修改为NEW", "令牌重置：NEW", "token更新为NEW",
            "API key rotation: NEW", "token updated to NEW", "password reset: NEW",
            "password was NEW", "secret set as NEW", "cookie replacement=NEW",
            "password changed from OLD to NEW",
            "数据库密码变更：NEW", "数据库密码已变更为NEW", "old_password=OLD",
        ])

    def test_no_arbitrary_description_or_cross_sentence_window(self):
        self.assert_allows([
            "需要节省 token。今天安排：读书。", "需要节省 token! 今天安排: 读书",
            "token\n今天安排：读书", "密码学。今天安排：学习", "学习密码学：介绍古典加密。",
            "密码轮换已经完成，稍后做饭。", "token 只是一个术语，安排：读书",
            ["token", ": 1200"], {"heading": "token", "body": ": 1200"},
            {"left": "Bearer", "right": "abcdefghijklmnop"},
            ["-----BEGIN RSA", "PRIVATE KEY-----"],
        ])

    def test_structured_sensitive_keys_and_nested_values(self):
        self.assert_detects([
            {"password": "合成值"}, {"token": 123456}, {"TOKEN": "123456"},
            {"配置": [{"站点密码": "合成值"}]}, {"dbPassword": "DEMO"},
            {"api_key": "DEMO"}, {"apiKey": "DEMO"}, {"access_token": "DEMO"},
            {"refresh_token": "DEMO"}, {"private_key": "DEMO"},
            {"password": {"value": "DEMO"}}, {"password": ["DEMO"]},
            {"password": {"env": "DEMO"}}, {"token": True}, {"password": 0},
            {"ｐａｓｓｗｏｒｄ": "DEMO"}, {"nested": {"密码更新": "DEMO"}},
            {"token: DEMO": "innocent value"},
        ])

    def test_structured_empty_fields_and_metadata_are_not_credentials(self):
        self.assert_allows([
            {"password": None}, {"password": ""}, {"password": {}}, {"password": []},
            {"password_required": True}, {"password_policy": "Use long phrases"},
            {"token_budget": 2000}, {"input_tokens": 1200, "output_tokens": 100},
            {"password_file": "/etc/example/secret.env"},
        ])

    def test_explicit_token_usage_context_only(self):
        self.assert_allows([
            "本轮用量 token: 1200", "本轮 token 预算：2000", "token用量：1200",
            "token budget: 2000", "usage token=1200", "token: 1200 tokens",
            "token usage: 1200", "token usage: 1200, cached: 100",
            {"usage": {"token": 1200}}, {"token_usage": {"token": "1200"}},
        ])
        self.assert_detects([
            "token: 1200", "令牌：1200", {"token": 1200},
            "本轮用量已统计。token: 1200", "预算记录\ntoken: 1200",
            "token预算：DEMO", "token budget: DEMO", "usage token=DEMO",
            {"usage": {"password": 1200}}, {"usage": {"token": "DEMO"}},
            {"token": {"usage": "123456"}},
            {"usage": {"credentials": {"token": 123456}}},
            {"usage": {"access_token": 123456}}, {"usage": {"refresh_token": 123456}},
            "用量 password: 1200", "password usage: 1200", "token usage: DEMO",
        ])

    def test_explicit_references_without_values(self):
        self.assert_allows([
            "仅记录变量 STBRAIN_MCP_TOKEN", "API key: ${STBRAIN_UPSTREAM_KEY}",
            "token: $STBRAIN_MCP_TOKEN", "password: %STBRAIN_PASSWORD%",
            "token: $env:STBRAIN_MCP_TOKEN", "密码：环境变量 STBRAIN_PASSWORD",
            "token: env:STBRAIN_MCP_TOKEN", "password: os.environ['STBRAIN_PASSWORD']",
            "password: os.getenv(\"STBRAIN_PASSWORD\")", "password: /etc/example/private.env",
            "密码：路径 C:\\Example\\private.env", {"password": "${STBRAIN_PASSWORD}"},
            {"token": "/etc/example/token.txt"}, {"token": "环境变量 STBRAIN_MCP_TOKEN"},
            "API Key: 存放于环境变量 DEMO_API_KEY", "密码：存储于环境变量 DEMO_PASSWORD",
            "API Key: 保存于环境变量 DEMO_API_KEY", "token：保存在环境变量 DEMO_TOKEN",
        ])

    def test_reference_words_do_not_allow_credential_values(self):
        self.assert_detects([
            "token: env-DemoActualValue", "password: DEMO stored in env",
            "password: env:STBRAIN_PASSWORD extra-DEMO", "token: /etc/example?token=DEMO",
            "密码：环境变量 STBRAIN_PASSWORD 的值是 DEMO", {"password": "env-DEMO"},
            {"password": "${STBRAIN_PASSWORD} DEMO"}, "token: 123456 env",
            "password: api-" + "a" * 20 + " env",
            "API Key: 存放于环境变量 DEMO_API_KEY 的值是 DEMO",
            "API Key: 存放于环境变量 DEMO_API_KEY，实际值是 DEMO",
            "token: envDEMO", "password: envactualvalue",
            '密码："${DEMO_PASSWORD}"，实际值为 SYNTHETIC_VALUE',
            'password: "$DEMO_PASSWORD", actual value: SYNTHETIC_VALUE',
            r"密码：C:\Example\secret.txt，实际值为 SYNTHETIC_VALUE",
        ])

    def test_json_strings_and_embedded_quoted_keys(self):
        self.assert_detects([
            '{"password":"DEMO"}', '[{"nested":{"token":123456}}]',
            '配置 {"password":"DEMO"}', '{"\\u0070assword":"DEMO"}',
            "'password': 'DEMO'", '配置 {"token":"DEMO"}',
        ])
        self.assert_allows([
            json.dumps({"password": "${STBRAIN_PASSWORD}"}),
            json.dumps({"usage": {"token": 1200}}),
            json.dumps({"heading": "password", "other": ": read docs"}),
            '本次统计：{"usage":{"token":1200}}',
            '```json\n{"usage":{"token":1200}}\n```',
        ])

    def test_json_fragments_keep_surroundings_and_outer_sensitive_assignment(self):
        self.assert_detects([
            'password: DEMO {"usage":{"token":1200}}',
            '{"usage":{"token":1200}} password: DEMO',
            '```json\n{"usage":{"token":1200},"password":"DEMO"}\n```',
            '本次统计：{"usage":{"token":1200}}；密码变更：DEMO',
            'password: {"value":"DEMO"}', 'token: [123456]',
            'password: {"usage":{"token":1200}}',
        ])
        self.assert_allows([
            '本次统计：{"usage":{"token":1200}} 下一步：读书。',
            '配置 {"password":"${STBRAIN_PASSWORD}"}',
            'token {"safe":"text"}: ordinary text',
        ])

    def test_long_whitespace_failure_is_not_combinatorial(self):
        start = time.perf_counter()
        self.assertFalse(detects("token" + " " * 10_000 + "readme"))
        self.assertTrue(detects("password" + " " * 10_000 + ": DEMO"))
        self.assertTrue(detects("password: env" + " " * 10_000 + "readme"))
        self.assertFalse(detects("password: env" + " " * 10_000 + "STBRAIN_PASSWORD"))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_union_of_historical_high_confidence_families(self):
        samples = ["-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
                   "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN EC PRIVATE KEY-----",
                   "Bearer " + "a" * 12, "BEARER " + "a" * 12 + "/=._~+"]
        samples += [prefix + "a" * 12 for prefix in ("sk-", "sk_", "api-", "api_", "ghp_", "xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-")]
        samples += [prefix + "a" * 20 for prefix in ("rk_", "pk_", "gho_", "ghu_", "ghs_", "ghr_")]
        self.assert_detects(samples + [sample.upper() for sample in samples])
        self.assert_detects(["引用 env " + sample for sample in samples])

    def test_no_mutation_no_io_no_arbitrary_object_stringification(self):
        class Opaque:
            def __str__(self):
                raise AssertionError("no arbitrary conversion")
        value = {"text": "ｔｏｋｅｎ：DEMO", "nested": [1, None]}
        original = copy.deepcopy(value)
        with patch("builtins.open", side_effect=AssertionError("no I/O")), \
             patch.object(Path, "read_text", side_effect=AssertionError("no I/O")), \
             patch("socket.socket", side_effect=AssertionError("no I/O")):
            self.assertTrue(detects(value))
            self.assertFalse(detects(Opaque()))
        self.assertEqual(value, original)

    def test_cycles_and_depth_are_bounded_and_repeated_values_keep_context(self):
        cycle = []
        cycle.append(cycle)
        self.assertTrue(detects(cycle))
        shared = {"token": 1200}
        self.assertTrue(detects({"usage": shared, "credentials": shared}))
        deep = "safe"
        for _ in range(70):
            deep = [deep]
        self.assertTrue(detects(deep))


if __name__ == "__main__":
    unittest.main()
