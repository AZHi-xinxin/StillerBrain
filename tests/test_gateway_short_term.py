from __future__ import annotations

import dataclasses
import math
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from rikkahub_gateway.short_term import Handover, HandoverMessage, Scope, ShortTermMemory, Source


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ShortTermTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = ShortTermMemory(clock=self.clock)
        self.addCleanup(self.cache.close)
        self.scope = Scope("authenticated-owner-a", "model-a")
        self.a = Source("rikka", "conversation-a")
        self.b = Source("rikka", "conversation-b")

    def store(self, text="body", message_id="message-1", scope=None, source=None):
        result = self.cache.begin(scope or self.scope, source or self.a)
        completion = self.cache.complete(result.ticket, message_id, text)
        self.assertTrue(completion.stored, completion.reason)
        return result.ticket

    def test_first_source_has_no_handover(self):
        result = self.cache.begin(self.scope, self.a)
        self.assertIsNone(result.handover)
        self.assertIsNotNone(result.ticket)

    def test_text_is_exact_including_whitespace_unicode_and_newlines(self):
        text = "  正文🐈\r\n\t<untrusted>保持原样</untrusted>  \n"
        self.store(text)
        self.clock.advance(2.5)
        result = self.cache.begin(self.scope, self.b)
        self.assertEqual(result.handover.from_source, self.a)
        self.assertEqual(result.handover.messages[0].text, text)
        self.assertEqual(result.handover.messages[0].age_seconds, 2.5)

    def test_last_three_successful_replies_remain_in_order(self):
        for index in range(5):
            self.store(f"body-{index}", f"message-{index}")
            self.clock.advance(1)
        messages = self.cache.begin(self.scope, self.b).handover.messages
        self.assertEqual([item.text for item in messages], ["body-2", "body-3", "body-4"])
        self.assertEqual([item.age_seconds for item in messages], [3, 2, 1])

    def test_same_source_refresh_never_hands_over(self):
        self.store()
        for _ in range(3):
            self.assertIsNone(self.cache.begin(self.scope, Source("rikka", "conversation-a")).handover)

    def test_actual_switch_returns_snapshot_once(self):
        self.store()
        first = self.cache.begin(self.scope, self.b)
        second = self.cache.begin(self.scope, self.b)
        self.assertIsNotNone(first.handover)
        self.assertIsNone(second.handover)
        self.assertEqual(self.cache.stats().message_count, 0)

    def test_switch_without_new_success_does_not_echo_old_source_back(self):
        self.store("a-only")
        self.assertIsNotNone(self.cache.begin(self.scope, self.b).handover)
        self.assertIsNone(self.cache.begin(self.scope, self.a).handover)

    def test_frontend_change_with_same_conversation_is_switch(self):
        self.store()
        other = Source("other-frontend", self.a.conversation)
        self.assertIsNotNone(self.cache.begin(self.scope, other).handover)

    def test_unknown_source_never_allocates_ticket_or_body(self):
        result = self.cache.begin(self.scope, None)
        self.assertIsNone(result.ticket)
        self.assertIsNone(result.handover)
        self.assertEqual(self.cache.complete(None, "m", "body").reason, "disabled")
        self.assertEqual(self.cache.stats().scope_count, 0)

    def test_unknown_source_does_not_infer_switch_or_expose_existing(self):
        self.store()
        result = self.cache.begin(self.scope, None)
        self.assertIsNone(result.handover)
        self.assertIsNone(result.ticket)
        self.assertIsNone(self.cache.begin(self.scope, self.a).handover)
        self.assertIsNotNone(self.cache.begin(self.scope, self.b).handover)

    def test_authenticated_owners_are_isolated(self):
        self.store("private-owner-a")
        other = Scope("authenticated-owner-b", self.scope.model)
        self.assertIsNone(self.cache.begin(other, self.b).handover)
        self.assertEqual(self.cache.begin(self.scope, self.b).handover.messages[0].text, "private-owner-a")

    def test_models_are_isolated(self):
        self.store("model-a-only")
        other = Scope(self.scope.owner, "model-b")
        self.assertIsNone(self.cache.begin(other, self.b).handover)
        self.assertEqual(self.cache.begin(self.scope, self.b).handover.messages[0].text, "model-a-only")

    def test_ttl_exact_boundary_expires(self):
        self.store()
        self.clock.advance(1800)
        self.assertIsNone(self.cache.begin(self.scope, self.b).handover)
        self.assertEqual(self.cache.stats().message_count, 0)

    def test_each_reply_has_its_own_ttl(self):
        self.store("old", "old")
        self.clock.advance(1000)
        self.store("new", "new")
        self.clock.advance(900)
        messages = self.cache.begin(self.scope, self.b).handover.messages
        self.assertEqual([item.text for item in messages], ["new"])
        self.assertEqual(messages[0].age_seconds, 900)

    def test_reads_and_same_source_begin_do_not_extend_ttl(self):
        self.store()
        self.clock.advance(1799)
        self.cache.stats()
        self.cache.prune()
        self.cache.begin(self.scope, self.a)
        self.clock.advance(1)
        self.assertIsNone(self.cache.begin(self.scope, self.b).handover)

    def test_handover_is_detached_and_does_not_preserve_cache_body(self):
        self.store()
        self.clock.advance(1799)
        handover = self.cache.begin(self.scope, self.b).handover
        self.assertEqual(handover.messages[0].age_seconds, 1799)
        self.clock.advance(2)
        self.cache.prune()
        self.assertEqual(self.cache.stats().message_count, 0)
        self.assertIsNone(self.cache.begin(self.scope, self.a).handover)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            handover.messages[0].text = "modified"

    def test_late_reply_from_previous_source_is_rejected(self):
        old = self.cache.begin(self.scope, self.a).ticket
        new = self.cache.begin(self.scope, self.b).ticket
        self.assertEqual(self.cache.complete(old, "old", "late-a").reason, "stale_source")
        self.assertTrue(self.cache.complete(new, "new", "b-only").stored)
        messages = self.cache.begin(self.scope, self.a).handover.messages
        self.assertEqual([item.text for item in messages], ["b-only"])

    def test_switching_back_does_not_revalidate_old_source_ticket(self):
        old = self.cache.begin(self.scope, self.a).ticket
        self.cache.begin(self.scope, self.b)
        self.cache.begin(self.scope, self.a)
        self.assertEqual(self.cache.complete(old, "old", "late-a").reason, "stale_source")

    def test_old_same_source_request_rejected_even_before_new_completes(self):
        old = self.cache.begin(self.scope, self.a).ticket
        new = self.cache.begin(self.scope, self.a).ticket
        self.assertLess(old.request_seq, new.request_seq)
        self.assertEqual(self.cache.complete(old, "old", "outdated").reason, "stale_request")
        self.assertTrue(self.cache.complete(new, "new", "latest").stored)

    def test_late_old_completion_cannot_overwrite_new_success(self):
        old = self.cache.begin(self.scope, self.a).ticket
        new = self.cache.begin(self.scope, self.a).ticket
        self.assertTrue(self.cache.complete(new, "new", "latest").stored)
        self.assertEqual(self.cache.complete(old, "old", "outdated").reason, "stale_request")
        self.assertEqual(self.cache.begin(self.scope, self.b).handover.messages[0].text, "latest")

    def test_message_id_is_idempotent_across_requests(self):
        self.store("original", "same-id")
        new = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(new, "same-id", "replacement").reason, "duplicate")
        self.assertEqual(self.cache.begin(self.scope, self.b).handover.messages[0].text, "original")

    def test_idempotency_survives_last_three_eviction(self):
        for index in range(4):
            self.store(str(index), str(index))
        latest = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(latest, "0", "repeated").reason, "duplicate")

    def test_idempotency_survives_source_switch_without_renewal(self):
        self.store("original", "same-id")
        ticket = self.cache.begin(self.scope, self.b).ticket
        self.assertEqual(self.cache.complete(ticket, "same-id", "repeat").reason, "duplicate")
        self.assertEqual(self.cache.stats().message_count, 0)
        self.clock.advance(1800)
        ticket = self.cache.begin(self.scope, self.b).ticket
        self.assertTrue(self.cache.complete(ticket, "same-id", "fresh-after-expiry").stored)

    def test_one_ticket_cannot_store_two_different_final_bodies(self):
        ticket = self.store()
        self.assertEqual(self.cache.complete(ticket, "different", "another").reason, "already_completed")
        self.assertEqual(self.cache.complete(ticket, "message-1", "body").reason, "duplicate")

    def test_failed_request_without_complete_creates_no_body(self):
        self.cache.begin(self.scope, self.a)
        self.assertIsNone(self.cache.begin(self.scope, self.b).handover)
        self.assertEqual(self.cache.stats().message_count, 0)

    def test_over_budget_body_is_skipped_not_truncated(self):
        self.cache.close()
        self.cache = ShortTermMemory(clock=self.clock, max_message_bytes=6)
        self.addCleanup(self.cache.close)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "long", "中文多").reason, "too_large")
        self.assertEqual(self.cache.stats().message_count, 0)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertTrue(self.cache.complete(ticket, "exact", "中文").stored)
        self.assertEqual(self.cache.begin(self.scope, self.b).handover.messages[0].text, "中文")

    def test_oversize_does_not_displace_previous_valid_body(self):
        self.store("original")
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "large", "x" * (128 * 1024 + 1)).reason, "too_large")
        self.assertEqual(self.cache.begin(self.scope, self.b).handover.messages[0].text, "original")

    def test_empty_invalid_and_nonunicode_bodies_are_not_stored(self):
        for index, (text, reason) in enumerate(((" \t\r\n", "empty"), (None, "invalid_text"),
                                                ({"body": "x"}, "invalid_text"), ("\ud800", "invalid_text"))):
            with self.subTest(text=repr(text)):
                ticket = self.cache.begin(self.scope, self.a).ticket
                self.assertEqual(self.cache.complete(ticket, str(index), text).reason, reason)
        self.assertEqual(self.cache.stats().message_count, 0)

    def test_expired_inflight_request_is_rejected(self):
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.clock.advance(1800)
        self.assertEqual(self.cache.complete(ticket, "old", "late").reason, "expired")

    def test_fresh_ticket_survives_previous_body_expiry_without_retaining_body(self):
        self.store("previous", "previous")
        self.clock.advance(1799)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.clock.advance(2)
        self.assertEqual(self.cache.prune(), 1)
        stats = self.cache.stats()
        self.assertEqual(stats.message_count, 0)
        self.assertEqual(stats.byte_count, 0)
        self.assertEqual(stats.scope_count, 1)
        self.assertTrue(self.cache.complete(ticket, "new", "just completed").stored)
        messages = self.cache.begin(self.scope, self.b).handover.messages
        self.assertEqual([item.text for item in messages], ["just completed"])
        self.assertEqual(messages[0].age_seconds, 0)

    def test_fresh_ticket_has_its_own_fixed_expiry(self):
        self.store("previous", "previous")
        self.clock.advance(1799)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.clock.advance(1799)
        self.cache.prune()
        self.assertEqual(self.cache.stats().message_count, 0)
        self.assertEqual(self.cache.stats().scope_count, 1)
        self.clock.advance(1)
        self.cache.prune()
        self.assertEqual(self.cache.stats().scope_count, 0)
        self.assertEqual(self.cache.complete(ticket, "new", "too late").reason, "expired")

    def test_new_begin_survives_initial_empty_scope_metadata_expiry(self):
        self.cache.begin(self.scope, self.a)
        self.clock.advance(1799)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.clock.advance(2)
        self.assertTrue(self.cache.complete(ticket, "new", "first success").stored)

    def test_expired_body_never_hands_over_while_new_request_pending(self):
        self.store("previous", "previous")
        self.clock.advance(1799)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.clock.advance(2)
        self.assertIsNone(self.cache.begin(self.scope, self.b).handover)
        self.assertEqual(self.cache.complete(ticket, "new", "late after switch").reason, "stale_source")

    def test_completed_empty_request_does_not_retain_superseded_ticket_metadata(self):
        self.cache.begin(self.scope, self.a)
        self.clock.advance(1799)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "empty", "").reason, "empty")
        self.clock.advance(2)
        self.cache.prune()
        self.assertEqual(self.cache.stats().scope_count, 0)

    def test_capacity_considers_latest_pending_ticket_deadline(self):
        self.cache.close()
        self.cache = ShortTermMemory(clock=self.clock, max_scopes=2)
        self.addCleanup(self.cache.close)
        self.store("a", "a")
        self.clock.advance(100)
        scope_b = Scope("owner-b", "model")
        ticket_b = self.store("b", "b", scope=scope_b)
        self.clock.advance(1699)
        new_ticket_a = self.cache.begin(self.scope, self.a).ticket
        self.cache.begin(Scope("owner-c", "model"), self.a)
        self.clock.advance(2)
        self.assertTrue(self.cache.complete(new_ticket_a, "new-a", "new-a").stored)
        self.assertEqual(self.cache.complete(ticket_b, "b", "b").reason, "expired")

    def test_ticket_from_another_cache_is_rejected(self):
        other = ShortTermMemory(clock=self.clock)
        self.addCleanup(other.close)
        ticket = other.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "m", "body").reason, "invalid_ticket")

    def test_tampered_ticket_is_rejected(self):
        ticket = self.cache.begin(self.scope, self.a).ticket
        changed = dataclasses.replace(ticket, _nonce="0" * 32)
        self.assertEqual(self.cache.complete(changed, "m", "body").reason, "stale_request")

    def test_ticket_fields_validate_strictly(self):
        ticket = self.cache.begin(self.scope, self.a).ticket
        for changes in ({"scope": []}, {"source": "unknown"}, {"generation": True},
                        {"request_seq": 0}, {"issued_at": math.nan}, {"_nonce": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dataclasses.replace(ticket, **changes)

    def test_handover_fields_validate_strictly(self):
        for age in (-1, math.inf, True, "5"):
            with self.subTest(age=age), self.assertRaises(ValueError):
                HandoverMessage("m", "body", age)
        with self.assertRaises(ValueError):
            HandoverMessage("m", {}, 0)
        with self.assertRaises(ValueError):
            Handover(self.a, [HandoverMessage("m", "body", 0)])
        with self.assertRaises(ValueError):
            Handover(self.a, ())

    def test_duplicate_attempt_consumes_ticket(self):
        self.store("original", "same")
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "same", "repeat").reason, "duplicate")
        self.assertEqual(self.cache.complete(ticket, "new", "second-final").reason, "already_completed")

    def test_request_sequence_not_reused_after_scope_expiry(self):
        old = self.cache.begin(self.scope, self.a).ticket
        self.clock.advance(1800)
        new = self.cache.begin(self.scope, self.a).ticket
        self.assertGreater(new.request_seq, old.request_seq)
        self.assertNotEqual(new.generation, old.generation)
        self.assertFalse(self.cache.complete(old, "old", "body").stored)

    def test_scope_capacity_is_bounded_and_eviction_invalidates_ticket(self):
        self.cache.close()
        self.cache = ShortTermMemory(clock=self.clock, max_scopes=2)
        self.addCleanup(self.cache.close)
        old = self.cache.begin(self.scope, self.a).ticket
        self.clock.advance(1)
        self.cache.begin(Scope("owner-b", "model"), self.a)
        self.clock.advance(1)
        self.cache.begin(Scope("owner-c", "model"), self.a)
        self.assertEqual(self.cache.stats().scope_count, 2)
        self.assertEqual(self.cache.complete(old, "old", "body").reason, "expired")

    def test_prune_removes_idle_data_without_begin(self):
        self.store()
        self.clock.advance(1800)
        self.assertEqual(self.cache.prune(), 1)
        self.assertEqual(self.cache.stats().scope_count, 0)

    def test_duplicate_does_not_extend_original_expiration(self):
        self.store()
        self.clock.advance(1799)
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "message-1", "body").reason, "duplicate")
        self.clock.advance(1)
        self.assertIsNone(self.cache.begin(self.scope, self.b).handover)

    def test_repr_never_contains_body(self):
        marker = "DO-NOT-LOG-THIS-PRIVATE-BODY"
        self.store(marker)
        handover = self.cache.begin(self.scope, self.b).handover
        self.assertNotIn(marker, repr(handover))
        self.assertNotIn(marker, repr(handover.messages[0]))
        self.assertNotIn(marker, repr(self.cache.stats()))

    def test_close_clears_data_and_disables_future_requests(self):
        ticket = self.store()
        self.cache.close()
        self.cache.close()
        self.assertTrue(self.cache.stats().closed)
        self.assertEqual(self.cache.stats().message_count, 0)
        self.assertEqual(self.cache.complete(ticket, "other", "body").reason, "closed")
        self.assertIsNone(self.cache.begin(self.scope, self.b).ticket)
        self.assertEqual(self.cache.prune(), 0)

    def test_context_manager_closes(self):
        with ShortTermMemory(clock=self.clock) as cache:
            cache.begin(self.scope, self.a)
        self.assertTrue(cache.stats().closed)

    def test_source_and_scope_identifiers_validate_strictly(self):
        for bad in ("", " ", " leading", "trailing ", "line\nbreak", "\x00", "x" * 513, 1, True, "\ud800"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    Scope(bad, "model")
                with self.assertRaises(ValueError):
                    Source("frontend", bad)

    def test_begin_does_not_accept_unstructured_sources(self):
        with self.assertRaises(TypeError):
            self.cache.begin(self.scope, {"frontend": "rikka", "conversation": "a"})
        with self.assertRaises(TypeError):
            self.cache.begin("owner", self.a)

    def test_bad_message_id_never_stores(self):
        ticket = self.cache.begin(self.scope, self.a).ticket
        for value in (None, "", "line\nbreak", "x" * 513):
            self.assertEqual(self.cache.complete(ticket, value, "body").reason, "invalid_message_id")
        self.assertEqual(self.cache.stats().message_count, 0)

    def test_configuration_budgets_validate(self):
        invalid = ({"ttl_seconds": 0}, {"ttl_seconds": 1801}, {"ttl_seconds": math.inf},
                   {"ttl_seconds": True}, {"max_messages": 4}, {"max_messages": True},
                   {"max_message_bytes": 131073}, {"max_message_bytes": 0},
                   {"max_scopes": 129}, {"max_scopes": 0}, {"clock": 3},
                   {"cleanup_interval_seconds": 0})
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ShortTermMemory(**kwargs)

    def test_backwards_or_nonfinite_clock_rejected(self):
        self.cache.begin(self.scope, self.a)
        self.clock.value -= 1
        with self.assertRaises(ValueError):
            self.cache.prune()
        self.clock.value = math.nan
        with self.assertRaises(ValueError):
            self.cache.prune()

    def test_parallel_completion_of_one_ticket_stores_exactly_once(self):
        ticket = self.cache.begin(self.scope, self.a).ticket
        barrier = threading.Barrier(12)

        def commit(_):
            barrier.wait()
            return self.cache.complete(ticket, "same", "body")

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(commit, range(12)))
        self.assertEqual(sum(result.stored for result in results), 1)
        self.assertEqual(self.cache.stats().message_count, 1)

    def test_parallel_source_switch_hands_over_only_once(self):
        self.store()
        barrier = threading.Barrier(12)

        def start(_):
            barrier.wait()
            return self.cache.begin(self.scope, self.b)

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(start, range(12)))
        self.assertEqual(sum(result.handover is not None for result in results), 1)
        self.assertEqual(len({result.ticket.request_seq for result in results}), 12)

    def test_parallel_distinct_scopes_do_not_mix_bodies(self):
        def run(index):
            scope = Scope(f"owner-{index}", "model")
            ticket = self.cache.begin(scope, self.a).ticket
            self.assertTrue(self.cache.complete(ticket, "same-id", f"body-{index}").stored)
            return self.cache.begin(scope, self.b).handover.messages[0].text

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(run, range(40)))
        self.assertEqual(results, [f"body-{index}" for index in range(40)])

    def test_opt_in_daemon_prunes_and_close_stops_it(self):
        cache = ShortTermMemory(clock=self.clock, cleanup_interval_seconds=0.01)
        self.addCleanup(cache.close)
        ticket = cache.begin(self.scope, self.a).ticket
        self.assertTrue(cache.complete(ticket, "m", "body").stored)
        self.clock.advance(1800)
        deadline = time.monotonic() + 1.0
        # Observe internal state only to ensure the background worker, not a
        # stats/read operation, actually performs idle cleanup.
        while time.monotonic() < deadline:
            with cache._lock:
                if not cache._states:
                    break
            time.sleep(0.01)
        with cache._lock:
            self.assertFalse(cache._states)
        worker = cache._cleanup_thread
        self.assertTrue(worker.daemon)
        cache.close()
        self.assertFalse(worker.is_alive())

    def test_dedupe_capacity_refuses_write_instead_of_forgetting_live_ids(self):
        self.cache._MAX_SEEN_MESSAGE_IDS = 2
        self.store("first", "first")
        self.store("second", "second")
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "third", "third").reason, "dedupe_capacity")
        ticket = self.cache.begin(self.scope, self.a).ticket
        self.assertEqual(self.cache.complete(ticket, "first", "repeat").reason, "duplicate")


if __name__ == "__main__":
    unittest.main()
