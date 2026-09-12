"""Offline v2 host negotiation, persistence and Control tests with synthetic data.

These prove the host protocol, not provider acceptance of a trailing system frame.
No production database, private configuration or model endpoint is accessed.
"""
from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest import mock

from mcp_server.control_server import ControlApplication
from runtime.onboarding import (
    CONTEXT_LAYOUT_CONTRACT,
    TAIL_CONTEXT_LAYOUT_CONTRACT,
    ModuleOneOnboardingStore,
    OnboardingError,
    OPTIONAL_BRAIN_NOTICE,
    _canonical,
    _sha256,
)
from tests import test_context_layout as layout_fixtures


OFFER = {
    "contract": "stbrain-context-layout/2",
    "insertion_rule": "after-client-messages",
}


class TailContextV2RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.base = layout_fixtures.ContextLayoutTests(methodName="runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.store = self.base.store
        self.fixture = self.base.fixture
        self.scope = self.base.scope

    def prepare(self, wake, layout_fields=None, **changes):
        arguments = {
            **self.scope,
            "wake_id": wake["wake_id"],
            "wake_capability": wake["wake_capability"],
            "source_digest": "synthetic-source-digest",
            "host_contract_digest": "synthetic-host-contract-digest",
            **({"context_layout_offer": deepcopy(OFFER)} if layout_fields is None else layout_fields),
        }
        arguments.update(changes)
        return self.store.build_pre_generation_context(**arguments)

    def reopen(self):
        self.store = ModuleOneOnboardingStore(
            self.base.database,
            capability_secret=self.fixture.store.capability_secret,
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )

    def test_no_offer_remains_legacy_with_exact_old_hash_and_empty_metadata(self):
        wake = self.base.issue()
        first = self.prepare(wake, {})
        self.assertNotIn("context_bundle", first)
        self.assertEqual(_sha256(first["message"]), first["context_hash"])
        self.assertEqual({}, self.base.metadata(wake))
        self.reopen()
        reused = self.prepare(wake, {"context_layout": None})
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_old_field_keeps_v1_contract_and_cold_reopen_hash(self):
        wake = self.base.issue()
        fields = {"context_layout": deepcopy(self.base.layout)}
        first = self.prepare(wake, fields)
        self.assertEqual(CONTEXT_LAYOUT_CONTRACT, first["context_bundle"]["contract"])
        self.assertEqual(self.base.layout, first["context_bundle"]["layout"])
        self.reopen()
        reused = self.prepare(wake, fields)
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(first["context_bundle"], reused["context_bundle"])
        self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_offer_has_exact_v2_bundle_binding_and_no_injected_host_descriptor(self):
        wake = self.base.issue()
        first = self.prepare(wake)
        bundle = first["context_bundle"]
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, bundle["contract"])
        self.assertEqual(OFFER, bundle["layout"])
        self.assertEqual({
            **self.scope, "host_id": "host:layout", "thread_id": "thread:layout",
            "wake_id": wake["wake_id"], "source_digest": "synthetic-source-digest",
            "host_contract_digest": "synthetic-host-contract-digest",
        }, bundle["binding"])
        self.assertEqual({"role": "system", "content": OPTIONAL_BRAIN_NOTICE}, bundle["stable_message"])
        self.assertIsNone(bundle["dynamic_message"])
        self.assertEqual(_sha256(first["message"]), bundle["legacy_message_hash"])
        self.assertEqual(_sha256(bundle), first["context_hash"])
        self.assertEqual({"layout": OFFER, "hard_suppressed": False}, self.base.metadata(wake))
        self.assertNotIn("contract", first["message"]["content"])
        self.assertNotIn(wake["wake_capability"], _canonical(bundle))
        self.assertEqual("context_not_injected", self.base.confirm(wake, bundle["legacy_message_hash"])["decision"])
        self.assertEqual("injected", self.base.confirm(wake, first["context_hash"])["decision"])

    def test_invalid_offer_rejected_before_any_state_or_snapshot_mutation(self):
        wake = self.base.issue()
        before = self.store.state(**self.scope)
        variants = [
            None, {}, [], True, "tail-context-v2", deepcopy(self.base.layout),
            {**OFFER, "contract": "stbrain-context-layout/1"},
            {**OFFER, "contract": "stbrain-context-layout/unknown"},
            {**OFFER, "insertion_rule": "before-current-human"},
            {**OFFER, "insertion_rule": None},
            {**OFFER, "human_message_index": 0},
            {**OFFER, "extra": "synthetic-untrusted-data"},
        ]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaisesRegex(OnboardingError, "context_layout_offer_invalid"):
                self.prepare(wake, {"context_layout_offer": variant})
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))
        self.assertEqual(before, self.store.state(**self.scope))

    def test_two_present_fields_conflict_even_when_either_is_null(self):
        wake = self.base.issue()
        for old in (None, deepcopy(self.base.layout)):
            for offer in (None, deepcopy(OFFER)):
                with self.subTest(old=old, offer=offer), self.assertRaisesRegex(OnboardingError, "context_layout_fields_conflict"):
                    self.prepare(wake, {"context_layout": old, "context_layout_offer": offer})
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))

    def test_each_input_field_accepts_only_its_own_version(self):
        wake = self.base.issue()
        for fields in ({"context_layout": OFFER}, {"context_layout_offer": self.base.layout}):
            with self.subTest(fields=fields), self.assertRaises(OnboardingError):
                self.prepare(wake, fields)
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))

    def test_same_wake_never_switches_between_legacy_v1_and_v2(self):
        variants = [{}, {"context_layout": self.base.layout}, {"context_layout_offer": OFFER}]
        for index, selected in enumerate(variants):
            wake = self.base.issue(event=f"synthetic-version-{index}")
            first = self.prepare(wake, selected)
            self.reopen()
            for attempted in variants:
                if attempted == selected:
                    continue
                denied = self.prepare(wake, attempted)
                self.assertFalse(denied["may_generate"])
                self.assertEqual(["context_snapshot_mismatch"], denied["reason_codes"])
            reused = self.prepare(wake, selected)
            self.assertEqual("context_reused", reused["decision"])
            self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_v2_material_is_frozen_on_cold_reopen_and_new_source_frame(self):
        self.fixture.seed_tagless_named_learning()
        wake = self.base.issue()
        first = self.prepare(wake, source_frame={"query_text": "你还记得我们之前读的侍魔嘛？"})
        bundle = first["context_bundle"]
        self.assertIsNotNone(bundle["dynamic_message"])
        self.assertEqual(json.loads(first["message"]["content"]), {
            **json.loads(bundle["stable_message"]["content"]),
            **json.loads(bundle["dynamic_message"]["content"]),
        })
        self.base.confirm(wake, first["context_hash"])
        self.reopen()
        reused = self.prepare(wake, source_frame={"query_text": "Different synthetic topic must not refresh this wake."})
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(bundle, reused["context_bundle"])
        self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_source_host_contract_and_catalog_conflicts_do_not_replace_snapshot(self):
        wake = self.base.issue()
        tools = {"catalog_hash": "synthetic-catalog-one", "entries": []}
        first = self.prepare(wake, advertised_tools=tools)
        for changes in (
            {"source_digest": "synthetic-source-other"},
            {"host_contract_digest": "synthetic-host-other"},
            {"advertised_tools": {"catalog_hash": "synthetic-catalog-two", "entries": []}},
        ):
            arguments = {"advertised_tools": tools, **changes}
            denied = self.prepare(wake, **arguments)
            self.assertFalse(denied["may_generate"])
            self.assertEqual(["context_snapshot_mismatch"], denied["reason_codes"])
        self.assertEqual(first["context_hash"], self.prepare(wake, advertised_tools=tools)["context_hash"])

    def test_invalid_persisted_v2_descriptor_or_metadata_fails_closed_after_reopen(self):
        wake = self.base.issue()
        self.prepare(wake)
        variants = [
            {"layout": {**OFFER, "extra": True}, "hard_suppressed": False},
            {"layout": {**OFFER, "contract": "unknown"}, "hard_suppressed": False},
            {"layout": {**OFFER, "insertion_rule": "before-current-human"}, "hard_suppressed": False},
            {"layout": OFFER, "hard_suppressed": 0},
            {"layout": OFFER, "hard_suppressed": False, "extra": True},
        ]
        for variant in variants:
            with self.base.connect() as connection:
                connection.execute("UPDATE brain_context_snapshots SET context_layout_json=? WHERE wake_id=?", (_canonical(variant), wake["wake_id"]))
            self.reopen()
            denied = self.prepare(wake)
            self.assertFalse(denied["may_generate"])
            self.assertEqual(["context_layout_metadata_invalid"], denied["reason_codes"])

    def test_valid_but_changed_persisted_version_is_not_rehashed(self):
        wake = self.base.issue()
        self.prepare(wake)
        with self.base.connect() as connection:
            connection.execute("UPDATE brain_context_snapshots SET context_layout_json=? WHERE wake_id=?", (
                _canonical({"layout": self.base.layout, "hard_suppressed": False}), wake["wake_id"],
            ))
        self.reopen()
        denied = self.prepare(wake, {"context_layout": self.base.layout})
        self.assertFalse(denied["may_generate"])
        self.assertEqual(["context_snapshot_hash_mismatch"], denied["reason_codes"])

    def test_changed_material_fails_closed_instead_of_minting_new_hash(self):
        wake = self.base.issue()
        self.prepare(wake)
        with self.base.connect() as connection:
            connection.execute("UPDATE brain_context_snapshots SET dynamic_json=? WHERE wake_id=?", (
                '{"synthetic_forgery":"not approved"}', wake["wake_id"],
            ))
        self.reopen()
        denied = self.prepare(wake)
        self.assertFalse(denied["may_generate"])
        self.assertEqual(["context_snapshot_hash_mismatch"], denied["reason_codes"])

    def test_hard_and_soft_off_preserve_committed_empty_system_on_cold_reopen(self):
        self.fixture.bootstrap_live()
        for mode in ("hard_off", "soft_off"):
            wake = self.base.issue(event=f"synthetic-{mode}")
            with mock.patch.object(self.store.injection_control_store, "effective_mode", return_value=mode):
                first = self.prepare(wake)
            self.assertEqual({"role": "system", "content": "{}"}, first["message"])
            self.assertEqual(first["message"], first["context_bundle"]["stable_message"])
            self.assertIsNone(first["context_bundle"]["dynamic_message"])
            self.reopen()
            reused = self.prepare(wake)
            self.assertEqual("context_reused", reused["decision"])
            self.assertEqual(first["context_bundle"], reused["context_bundle"])
            self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_disabled_injection_bypasses_without_snapshot(self):
        self.store.ensure_state(**self.scope)
        with self.base.connect() as connection:
            connection.execute("UPDATE brain_onboarding_state SET injection_policy='disabled'")
        wake = self.base.issue()
        result = self.prepare(wake)
        self.assertEqual("bypass", result["decision"])
        self.assertNotIn("context_bundle", result)
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))

    def test_dynamic_only_projection_keeps_exact_fields_and_collision_guard(self):
        wake = {**self.scope, "host_id": "synthetic-host", "thread_id": "synthetic-thread", "wake_id": "synthetic-wake"}
        dynamic = {"learning_memory": {"summary": "Exact synthetic memory text."}}
        message = self.store._context_message(stable={}, dynamic=dynamic)
        arguments = dict(stable={}, dynamic=dynamic, message=message, layout=OFFER, wake=wake,
                         source_digest="synthetic-source", host_contract_digest="synthetic-host-contract")
        bundle = self.store._context_bundle(**arguments)
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, bundle["contract"])
        self.assertIsNone(bundle["stable_message"])
        self.assertEqual(message, bundle["dynamic_message"])
        with self.assertRaisesRegex(OnboardingError, "context_layout_projection_collision"):
            self.store._context_bundle(**{**arguments, "stable": dynamic})

    def test_cross_owner_wrong_capability_and_superseded_wake_remain_denied(self):
        wake = self.base.issue()
        self.prepare(wake)
        with self.assertRaises(OnboardingError):
            self.prepare(wake, owner_id="synthetic-other-owner")
        self.assertEqual("wake_invalid", self.prepare(wake, wake_capability="synthetic-invalid-capability")["decision"])
        self.base.issue(event="synthetic-later", thread="synthetic-other-thread")
        self.assertEqual("wake_superseded", self.prepare(wake)["decision"])

    def test_control_authenticates_offer_and_exact_bundle_confirmation(self):
        wake = self.base.issue()
        token = "synthetic-host-token-" + "h" * 32
        app = self.control(token)
        body = self.control_body(wake, {"context_layout_offer": OFFER})
        self.assertEqual(401, self.post(app, body, token=None)[0])
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))
        status, first = self.post(app, body, token)
        self.assertEqual(200, status)
        self.assertEqual(OFFER, first["context_bundle"]["layout"])
        status, confirmed = app.handle("POST", "/v1/host/context/confirm", {
            "Content-Type": "application/json", "Authorization": "Bearer " + token,
        }, json.dumps({"wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"],
                       "context_hash": first["context_hash"]}).encode())
        self.assertEqual(200, status)
        self.assertEqual("injected", confirmed["decision"])

    def test_control_rejects_ambiguous_or_invalid_offer_before_store_call(self):
        wake = self.base.issue()
        token = "synthetic-host-token-" + "h" * 32
        app = self.control(token)
        cases = [
            {"context_layout": None, "context_layout_offer": OFFER},
            {"context_layout": self.base.layout, "context_layout_offer": None},
            {"context_layout": None, "context_layout_offer": None},
        ]
        with mock.patch.object(self.store, "build_pre_generation_context", side_effect=AssertionError("must reject before store")):
            for fields in cases:
                self.assertEqual(400, self.post(app, self.control_body(wake, fields), token)[0])
        for fields in ({"context_layout_offer": None}, {"context_layout_offer": self.base.layout},
                       {"context_layout_offer": {**OFFER, "extra": "synthetic"}}, {"context_layout": OFFER}):
            self.assertEqual(400, self.post(app, self.control_body(wake, fields), token)[0])
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))

    def test_control_absent_offer_and_explicit_old_null_remain_legacy(self):
        wake = self.base.issue()
        token = "synthetic-host-token-" + "h" * 32
        app = self.control(token)
        status, first = self.post(app, self.control_body(wake, {}), token)
        self.assertEqual(200, status)
        self.assertNotIn("context_bundle", first)
        status, reused = self.post(app, self.control_body(wake, {"context_layout": None}), token)
        self.assertEqual(200, status)
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(first["context_hash"], reused["context_hash"])

    def control(self, token):
        return ControlApplication(self.store, **self.scope, host_token=token,
                                  human_token="synthetic-human-token-" + "u" * 32,
                                  human_actor_id="synthetic-human")

    @staticmethod
    def control_body(wake, fields):
        return {"wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"],
                "source_digest": "synthetic-source-digest", "host_contract_digest": "synthetic-host-contract-digest", **fields}

    @staticmethod
    def post(app, body, token):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        return app.handle("POST", "/v1/host/context/prepare", headers, json.dumps(body).encode())


if __name__ == "__main__":
    unittest.main()
