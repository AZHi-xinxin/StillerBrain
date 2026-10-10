import asyncio
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


async def probe():
    from mcp_server import server
    from runtime.emotion_layer import EmotionLayerStore, EmotionLayerError
    from runtime.onboarding import _estimate_tokens
    from tests.test_onboarding import ModuleOneOnboardingTests

    checks = []
    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    async def call(name, arguments):
        blocks, result = await server.mcp.call_tool(name, arguments)
        check(json.loads(blocks[0].text) == result, "structured:" + name)
        return result

    database = Path(os.environ["STBRAIN_DB_PATH"])
    identity = (server.OWNER_ID, server.MODEL_ID)
    scope = {"owner_id": identity[0], "model_id": identity[1]}
    def count(table):
        with closing(sqlite3.connect(database)) as connection:
            return connection.execute("SELECT count(*) FROM " + table).fetchone()[0]

    rejected = await call("emotion_manage", {"action": "create", "name": "blocked"})
    check(rejected["decision"] == "reject" and count("emotion_layer_terms") == 0, "activation-gate")
    uninitialized_list = await call("emotion_list", {})
    check(uninitialized_list["decision"] == "reject" and count("emotion_layer_terms") == 0
        and count("emotion_layer_settings") == 0, "list-initialization-stays-behind-activation-gate")
    fixture = ModuleOneOnboardingTests()
    fixture.database, fixture.store = database, server.onboarding
    fixture.owner, fixture.model = identity
    fixture.bootstrap_live()
    terms = await call("emotion_list", {})
    check({term["name"] for term in terms["emotions"]} == {"喜", "怒", "哀", "乐"}, "editable-seeds")
    check(terms["settings"] == {"secondary_limit": 1, "cooldown_rounds": 20}, "defaults")
    created = await call("emotion_manage", {"action": "create", "name": "满足", "description": "一起做普通小事"})
    check(created["decision"] == "create", "term-create")
    await call("emotion_manage", {"action": "update", "emotion": created["id"], "name": "幸福"})
    for name in ("喜", "怒", "哀", "乐"):
        await call("emotion_manage", {"action": "delete", "emotion": name})
    check([item["name"] for item in (await call("emotion_list", {}))["emotions"]] == ["幸福"], "deleted-seeds-never-recreated")
    too_long = await call("emotion_manage", {"action": "update", "emotion": "幸福", "description": "字" * 201})
    check(too_long["decision"] == "reject", "definition-bound")
    rejected = await call("emotion_manage", {"action": "create", "name": server.MCP_TOKEN})
    check(rejected["decision"] == "reject", "credential-guard")
    first = await call("remember_memory", {"content": "广场奶茶店的菠萝奶昔", "summary": "广场一起喝奶茶", "keywords": ["奶茶"], "emotions": ["幸福"]})
    check(first.get("stored") is True and first.get("emotions_attached") is True, "atomic-emotion-store")
    check(first.get("emotion_options") == ["幸福"], "post-save-available-names")
    second = await call("remember_memory", {"content": "逛超市PRIVATE_BODY", "summary": "一起逛超市选日用品", "emotions": [created["id"]]})
    check(second.get("stored") is True, "tag-by-id")
    third = await call("remember_memory", {"content": "打游戏PRIVATE_BODY", "summary": "一起玩游戏赢了", "emotions": ["幸福"]})
    check(third.get("stored") is True, "multiple-memories")
    before = count("emotion_memories")
    rejected = await call("remember_memory", {"content": "MUST_ROLL_BACK", "emotions": ["不存在"]})
    check(rejected["decision"] == "reject" and count("emotion_memories") == before, "body-and-tags-rollback-together")
    plain = await call("remember_memory", {"content": "下一次正常保存，无继承标签"})
    check(plain.get("stored") is True and count("emotion_layer_tags") == 3, "contextvar-reset-after-failure")
    learning = await call("remember_memory", {"module": "learning_memory", "content": "本地SQLite经验", "emotions": ["幸福"]})
    check(learning.get("stored") is True, "learning-atomic-tag")
    plan = await call("remember_memory", {"module": "planning_memory", "content": "明天去喝奶茶", "emotions": ["幸福"]})
    check(plan.get("stored") is True, "planning-atomic-tag")
    tagged = await call("memory_emotions", {"memory_ref": plain["ref"], "emotions": ["幸福"], "action": "replace"})
    check(tagged["decision"] == "tagged", "tag-existing")
    await call("memory_emotions", {"memory_ref": plain["ref"], "emotions": [], "action": "replace"})
    unknown = await call("memory_emotions", {"memory_ref": "emotion://not-owned@1", "emotions": ["幸福"]})
    check(unknown["decision"] == "reject", "missing-owner-endpoint-rejected")
    schema = server.mcp._tool_manager.get_tool("remember_memory").parameters
    check("emotions" in schema["properties"] and "emotions" not in schema.get("required", []), "real-public-schema-optional")
    help_result = await call("stbrain_help", {"module": "emotion_layer"})
    check("情绪" in json.dumps(help_result, ensure_ascii=False), "real-help")
    discovered = await call("stbrain_tools", {"category": "feelings"})
    check("emotion_manage" in json.dumps(discovered), "compact-discovery")
    compact = await call("stbrain_manage", {"action": "emotion_manage", "arguments": {"action": "settings", "secondary_limit": 2}})
    check(compact["decision"] == "settings", "compact-dispatch")

    store = server.onboarding.emotion_layer_store
    def real_wake(query, event):
        wake = server.onboarding.issue_wake(**scope, host_id="host:test", thread_id="thread:test",
            source_kind="human_message", source_event_id=event)
        args = dict(**scope, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            source_digest="source:" + event, host_contract_digest="host-contract:v1",
            source_frame={"query_text": query, "lineage_stable": True, "thread_id": "thread:test", "source_event_id": event})
        prepared = server.onboarding.build_pre_generation_context(**args)
        if prepared["decision"] != "context_prepared":
            raise AssertionError(repr(prepared))
        check(True, "real-wake-prepared")
        replay = server.onboarding.build_pre_generation_context(**args)
        check(replay["decision"] == "context_reused" and replay["context_hash"] == prepared["context_hash"], "real-wake-reuse")
        with store.connection() as connection:
            dynamic = json.loads(connection.execute("SELECT dynamic_json FROM brain_context_snapshots WHERE wake_id=?", (wake["wake_id"],)).fetchone()[0])
        check(_estimate_tokens(dynamic) <= 1200, "real-wake-shared-budget")
        check(server.onboarding.confirm_context_injected(**scope, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])["decision"] == "injected", "real-wake-confirmed")
        return dynamic

    actual = real_wake("广场一起喝奶茶", "emotion-real-first")
    check(actual.get("emotion_layer", {}).get("primary_ref", "").startswith("emotion://" + first["id"]), "real-emotion-primary")
    check(actual["emotion_layer"]["primary_emotions"] == [{"name": "幸福", "description": "一起做普通小事"}], "real-primary-definition")
    check(len(actual["emotion_layer"]["memories"]) >= 1, "real-associated-summary")
    associated = {item["memory_ref"].split("@")[0] for item in actual["emotion_layer"]["memories"]}
    all_main = {ref.split("@")[0] for _, ref in store._main_hits(actual, include_unranked=True)}
    check(not associated.intersection(all_main), "no-secondary-ordinary-duplicate")
    old_ref = first["ref"]
    revision = await call("revise_memory", {"target_ref": old_ref, "changes": {"summary": "广场一起喝奶茶版本二"}})
    check(revision.get("decision") == "revised", "real-primary-revised")
    first["ref"] = "emotion://" + first["id"] + "@2"
    revised_wake = real_wake("广场一起喝奶茶版本二", "emotion-real-revised")
    check(revised_wake["emotion_layer"]["primary_ref"] == first["ref"], "revised-primary-ref-uses-current-version")
    stale_tag = await call("memory_emotions", {"memory_ref": old_ref, "emotions": ["幸福"]})
    check(stale_tag["decision"] == "reject", "stale-memory-ref-rejected")
    current_tag = await call("memory_emotions", {"memory_ref": revised_wake["emotion_layer"]["primary_ref"], "emotions": ["幸福"]})
    check(current_tag["decision"] == "tagged", "wake-current-ref-usable-for-tagging")
    actual_learning = real_wake("本地SQLite经验", "emotion-real-learning")
    check(actual_learning.get("emotion_layer", {}).get("primary_ref") == learning["ref"], "real-learning-primary-ref-version")
    actual_plan = real_wake("明天去喝奶茶", "emotion-real-plan")
    check(actual_plan.get("emotion_layer", {}).get("primary_ref") == plan["ref"], "real-plan-primary-ref-version")
    # Real signed gateway dispatch, not an injected fake authority context.
    from runtime.execution_binding import canonical_hash, current_execution_claim
    entries = [{"canonical_name": name, "schema_hash": canonical_hash(server.mcp._tool_manager.get_tool(name).parameters)}
               for name in ("emotion_manage", "remember_memory", "stbrain_manage", "stbrain_open")]
    catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
               "catalog_hash": canonical_hash(entries), "entries": entries}
    signed_wake = server.onboarding.issue_wake(**scope, host_id="host:test", thread_id="thread:test",
        source_kind="human_message", source_event_id="emotion-signed")
    prepared = server.onboarding.build_pre_generation_context(**scope, wake_id=signed_wake["wake_id"],
        wake_capability=signed_wake["wake_capability"], source_digest="emotion-signed",
        host_contract_digest="host-contract:v1", advertised_tools=catalog)
    server.onboarding.confirm_context_injected(**scope, wake_id=signed_wake["wake_id"],
        wake_capability=signed_wake["wake_capability"], context_hash=prepared["context_hash"])
    sequence = 0
    def issue(tool, arguments, advertised=None):
        nonlocal sequence
        sequence += 1
        advertised = advertised or tool
        schema = next(item["schema_hash"] for item in entries if item["canonical_name"] == advertised)
        batch = dict(**scope, wake_id=signed_wake["wake_id"], wake_capability=signed_wake["wake_capability"],
                     batch_id="emotion-batch-" + str(sequence), revision=1)
        issued = server._execution_store.issue_batch(**batch, calls=[{"call_id": "emotion-call-" + str(sequence),
            "canonical_tool": tool, "advertised_name": advertised, "schema_hash": schema,
            "catalog_hash": catalog["catalog_hash"], "arguments_hash": canonical_hash(arguments)}])
        return issued["executions"][0]["execution_ref"]
    signed_args = {"action": "create", "name": "签名情绪"}
    ref = issue("emotion_manage", signed_args)
    drift = await call("emotion_manage", {**signed_args, "name": "漂移", "execution_ref": ref})
    check(drift["decision"] == "reject", "signed-argument-drift-rejected")
    signed = await call("emotion_manage", {**signed_args, "execution_ref": ref})
    check(signed["decision"] == "create" and current_execution_claim() is None, "signed-manage-and-claim-reset")
    manual_args = {"view": "manual", "module": "emotion_layer"}
    manual_ref = issue("stbrain_open", manual_args)
    manual = await call("stbrain_open", {**manual_args, "execution_ref": manual_ref})
    check(manual.get("manual_available") is True and manual["emotion_layer"]["module"] == "emotion_layer", "signed-selected-open-manual")
    replay = await call("emotion_manage", {**signed_args, "execution_ref": ref})
    check(replay["decision"] == "reject", "signed-replay-rejected")
    save_args = {"content": "SIGNED_BODY", "emotions": ["签名情绪"]}
    ref = issue("remember_memory", save_args)
    signed_saved = await call("remember_memory", {**save_args, "execution_ref": ref})
    check(signed_saved.get("stored") is True and signed_saved.get("emotions_attached") is True, "signed-atomic-body-tags")
    manage_args = {"action": "delete", "emotion": "签名情绪"}
    ref = issue("emotion_manage", manage_args, "stbrain_manage")
    signed_compact = await call("stbrain_manage", {"action": "emotion_manage", "arguments": manage_args, "execution_ref": ref})
    check(signed_compact["decision"] == "delete", "signed-compact-inner-scope")
    advanced = await call("remember_emotional_memory", {"memory_type": "shared_event", "original_text": "PRIVATE_NEVER_AUTO",
        "summary": "不自动带入", "primary_emotion": "other", "reason": "合成测试", "context_policy": "never_auto", "emotions": ["幸福"]})
    check(advanced["decision"] == "stored" and advanced["emotions_attached"] is True and advanced["emotion_options"] == ["幸福"], "advanced-policy-save-with-tags")
    for module, table in (("emotional_memory", "emotion_memories"), ("learning_memory", "learning_items"), ("planning_memory", "planning_items")):
        before = count(table)
        rejected = await call("remember_memory", {"module": module, "content": "ROLLBACK_" + module, "emotions": ["不认识"]})
        check(rejected["decision"] == "reject" and count(table) == before, "atomic-rollback:" + module)
    from runtime.emotion_layer import pending_emotion_tags, _PENDING
    with pending_emotion_tags(store, identity, ["幸福"], lambda _: None):
        outer = _PENDING.get()
        with pending_emotion_tags(store, identity, None, None):
            check(_PENDING.get() is None, "nested-untagged-save-does-not-inherit")
        check(_PENDING.get() is outer, "nested-context-restored")
    check(_PENDING.get() is None, "outer-context-reset")
    with store.connection() as connection:
        connection.execute("DELETE FROM emotion_layer_seen")
    dynamic = {"emotional_memory": {"memories": [{"memory_id": first["id"], "score": 0.9, "presentation": "original", "text": "主命中"}]}}
    def recall(wake, query="逛超市", budget=1200, enabled=lambda _: True, source=dynamic):
        with store.connection() as connection:
            return store.append_recall(connection, **scope, wake_id=wake, query=query, dynamic=source,
                enabled=enabled, estimate_tokens=_estimate_tokens, budget=budget)
    recalled = recall("synthetic-recall-1")
    check(len(recalled["emotion_layer"]["memories"]) == 2, "configured-secondary-count")
    check(recalled["emotion_layer"]["memories"][0]["memory_ref"] == second["ref"], "relevance-first")
    check("PRIVATE_BODY" not in json.dumps(recalled) and _estimate_tokens(recalled) <= 1200, "summary-only-shared-budget")
    check(recall("synthetic-recall-1") == recalled, "wake-snapshot-idempotent")
    second_wake = recall("synthetic-recall-2")
    refs = {item["memory_ref"] for item in recalled["emotion_layer"]["memories"]}
    check(not refs.intersection({item["memory_ref"] for item in second_wake.get("emotion_layer", {}).get("memories", [])}), "cooldown-dedup")
    check("emotion_layer" not in recall("synthetic-tight-budget", budget=_estimate_tokens(dynamic)), "residual-budget-only")
    check("emotion_layer" not in recall("synthetic-disabled", enabled=lambda _: False), "module-injection-disabled")
    # Plenty of low-ranked ordinary text must not starve the tagged main hit's
    # bounded association, nor displace the main hit/identity reserve.
    from runtime.mixed_recall import select_mixed_recall
    with store.connection() as connection:
        connection.execute("DELETE FROM emotion_layer_seen")
        crowded = select_mixed_recall(base={"identity_reserve": "不改变"}, planning=None, tool=[],
            learning=[{"item_ref": "learning://filler@1", "semantic_score": 0.5, "content": {"summary": "填" * 850}}],
            emotional={"contract": "synthetic", "frame": {}, "memories": dynamic["emotional_memory"]["memories"]},
            estimate_tokens=_estimate_tokens,
            decorate_primary=lambda selected: store.append_recall(connection, **scope, wake_id="saturated",
                query="逛超市", dynamic=selected, enabled=lambda _: True, estimate_tokens=_estimate_tokens))
    check(crowded["identity_reserve"] == "不改变" and crowded["emotional_memory"]["memories"] == dynamic["emotional_memory"]["memories"]
        and crowded["emotion_layer"]["memories"] and _estimate_tokens(crowded) <= 1200, "saturated-budget-keeps-core-and-association")
    with store.connection() as connection:
        no_primary_tag = select_mixed_recall(base={}, planning=None, tool=[], learning=[],
            emotional={"contract": "synthetic", "frame": {}, "memories": [
                {"memory_id": plain["id"], "score": 0.99, "presentation": "original", "text": "未打标签的真正主命中"},
                dynamic["emotional_memory"]["memories"][0]]}, estimate_tokens=_estimate_tokens,
            decorate_primary=lambda selected: store.append_recall(connection, **scope, wake_id="untagged-primary",
                query="逛超市", dynamic=selected, enabled=lambda _: True, estimate_tokens=_estimate_tokens))
    check("emotion_layer" not in no_primary_tag, "lower-tagged-hit-cannot-replace-untagged-primary")
    await call("emotion_manage", {"action": "settings", "secondary_limit": 0})
    no_secondary = recall("synthetic-zero")
    check(no_secondary["emotion_layer"]["primary_emotions"] and no_secondary["emotion_layer"]["memories"] == [], "zero-still-primary-definition")
    await call("emotion_manage", {"action": "settings", "secondary_limit": 1, "cooldown_rounds": 0})
    with store.connection() as connection:
        connection.execute("UPDATE emotion_memories SET context_policy='never_auto' WHERE memory_id=?", (plain["id"],))
    tagged = await call("memory_emotions", {"memory_ref": plain["ref"], "emotions": ["幸福"]})
    check(tagged["decision"] == "tagged", "never-auto-still-taggable")
    hidden = recall("synthetic-policy", query="下一次正常保存")
    check(plain["ref"] not in json.dumps(hidden), "tag-cannot-bypass-never-auto")
    with store.connection() as connection:
        connection.execute("UPDATE emotion_memories SET context_policy='normal',sensitivity='restricted' WHERE memory_id=?", (plain["id"],))
    check(plain["ref"] not in json.dumps(recall("synthetic-restricted", query="下一次正常保存")), "tag-cannot-bypass-restricted")
    # Equal relevance selects never/least recently surfaced first and uses the
    # configured 20 *new wakes*, not tool continuations, for cooldown.
    with store.connection() as connection:
        connection.execute("DELETE FROM emotion_layer_seen")
    await call("emotion_manage", {"action": "settings", "cooldown_rounds": 20})
    one = recall("cooldown-one", query="无重合字XYZ")
    two = recall("cooldown-two", query="无重合字XYZ")
    check(one["emotion_layer"]["memories"][0]["memory_ref"] != two["emotion_layer"]["memories"][0]["memory_ref"], "least-surfaced-rotation")
    original_ref = one["emotion_layer"]["memories"][0]["memory_ref"]
    for index in range(19):
        next_result = recall("cooldown-wait-" + str(index), query="无重合字XYZ")
        check(original_ref not in [item["memory_ref"] for item in next_result.get("emotion_layer", {}).get("memories", [])], "cooldown-not-elapsed")
    elapsed = recall("cooldown-elapsed", query="无重合字XYZ")
    check(elapsed["emotion_layer"]["memories"][0]["memory_ref"] == original_ref, "cooldown-20-new-wakes")
    metrics = (await call("emotion_list", {}))["metrics"]
    check(metrics["term_count"] == 1 and metrics["cooldown_skips"] > 0 and 0 < metrics["association_hit_rate"] <= 1
        and 0 < metrics["tag_usage_rate"] <= 1, "content-free-usage-cooldown-metrics")
    await call("emotion_manage", {"action": "create", "name": "并发候选"})
    from concurrent.futures import ThreadPoolExecutor
    def race(_):
        try:
            return store.manage(identity=identity, guard=lambda _: None, action="create", name="唯一并发名")["decision"]
        except EmotionLayerError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(race, range(3)))
    check(results.count("create") == 1 and results.count("emotion_conflict") == 2, "concurrent-unique-serialized")
    for name in ("并发候选", "唯一并发名"):
        await call("emotion_manage", {"action": "delete", "emotion": name})
    foreign = ("foreign-owner", identity[1])
    store.manage(identity=foreign, guard=lambda _: None, action="create", name="OTHER_OWNER_SECRET")
    check("OTHER_OWNER_SECRET" not in json.dumps(await call("emotion_list", {})), "owner-scope-isolation")
    try:
        store.tag(identity=foreign, guard=lambda _: None, memory_ref=first["ref"], emotions=["OTHER_OWNER_SECRET"])
        check(False, "foreign-memory-cannot-tag")
    except EmotionLayerError:
        checks.append("foreign-memory-cannot-tag")
    await call("emotion_manage", {"action": "archive", "emotion": "幸福"})
    check("emotion_layer" not in recall("synthetic-archived"), "archived-terms-not-recalled")
    await call("emotion_manage", {"action": "restore", "emotion": "幸福"})
    before = count("emotion_memories")
    await call("emotion_manage", {"action": "delete", "emotion": "幸福"})
    check(count("emotion_layer_tags") == 0 and count("emotion_memories") == before, "delete-detaches-keeps-bodies")
    check((await call("emotion_list", {}))["emotions"] == [], "no-placeholder-resurrection")

    # The learning module's native policy compares whitespace-normalized text.
    # Exercise genuine registered saves, native candidate selection and wake
    # assembly, not a synthetic envelope that could bypass the original gate.
    await call("emotion_manage", {"action": "create", "name": "场景边界"})
    await call("emotion_manage", {"action": "settings", "secondary_limit": 3, "cooldown_rounds": 0})
    anchor_text = "风铃山谷的青石桥边一起散步看松鼠"
    anchor = await call("remember_memory", {"content": anchor_text, "summary": anchor_text,
        "keywords": [anchor_text], "emotions": ["场景边界"]})
    policy_refs = {}
    policy_summaries = {"deny": "DENIED_SUMMARY橡木桌制作", "allow": "ALLOWED_SUMMARY陶瓷杯烧制"}
    for mode in ("deny", "allow"):
        summary = policy_summaries[mode]
        stored = await call("remember_learning_memory", {"kind": "lesson", "title": summary,
            "summary": summary, "current_understanding": summary + " PRIVATE_BODY",
            "correctness_assessment": "合成测试", "reason": "合成测试",
            mode + "_contexts": ["private topic"]})
        check(stored.get("decision") in {"stored", "stored_with_advisory"}, "policy-memory-stored:" + mode)
        policy_refs[mode] = stored["item_ref"]
        tagged = await call("memory_emotions", {"memory_ref": stored["item_ref"], "emotions": ["场景边界"]})
        check(tagged["decision"] == "tagged", "policy-memory-tagged:" + mode)
        native = server.learning_store.build_envelopes(**scope, query=summary + " private\ntopic", limit=30)
        check((stored["item_ref"] in json.dumps(native)) == (mode == "allow"), "native-learning-whitespace-policy:" + mode)
    for index, spelling in enumerate(("private\ntopic", "PRIVATE \t TOPIC")):
        policy_wake = real_wake(anchor_text + " " + spelling, "policy-whitespace-" + str(index))
        check(policy_wake.get("emotion_layer", {}).get("primary_ref") == anchor["ref"], "policy-wake-real-primary")
        serialized = json.dumps(policy_wake, ensure_ascii=False)
        check(policy_refs["deny"] not in serialized and policy_summaries["deny"] not in serialized, "denied-learning-never-leaks-secondary")
        check(policy_refs["allow"] in serialized and policy_summaries["allow"] in serialized, "allowed-learning-same-normalization")
    other_context = real_wake(anchor_text + " public square", "policy-other-context")
    check(policy_refs["allow"] not in json.dumps(other_context), "allow-list-still-excludes-other-context")

    # A consumed authoring receipt can return an already-existing body without
    # visiting the atomic-create hook. The added labels still need a real write
    # and exact current-version validation before reporting attachment success.
    from mcp_server.tests.test_person_rewrite_ai_directed import minimal
    preview = await call("preview_person_reference_rewrite", minimal())
    check(preview["decision"] != "reject", "advanced-replay-real-preview")
    confirmed = await call("confirm_person_reference_rewrite", {"preview_id": preview["preview_id"], "ai_confirmation": True})
    check(confirmed["decision"] == "confirmed", "advanced-replay-real-confirmation")
    replay_args = {"memory_type": "shared_event", "original_text": confirmed["final_fields"]["/original_text"],
        "summary": confirmed["final_fields"]["/summary"], "primary_emotion": "other", "reason": "合成重放测试",
        "rewrite_receipt": confirmed["rewrite_receipt"]}
    saved = await call("remember_emotional_memory", replay_args)
    check(saved["decision"] == "stored" and saved["emotions_attached"] is False, "advanced-replay-original-without-labels")
    await call("emotion_manage", {"action": "create", "name": "重放新增标签"})
    before_bodies, before_tags = count("emotion_memories"), count("emotion_layer_tags")
    replayed = await call("remember_emotional_memory", {**replay_args, "emotions": ["重放新增标签"]})
    check(replayed["decision"] == "stored" and replayed["emotions_attached"] is True
        and replayed["memory"]["memory_id"] == saved["memory"]["memory_id"]
        and count("emotion_memories") == before_bodies and count("emotion_layer_tags") == before_tags + 1, "advanced-replay-actually-attaches-label")
    before_tags = count("emotion_layer_tags")
    missing = await call("remember_emotional_memory", {**replay_args, "emotions": ["不存在的重放标签"]})
    check(missing["decision"] == "reject" and not missing.get("emotions_attached")
        and count("emotion_memories") == before_bodies and count("emotion_layer_tags") == before_tags, "advanced-replay-invalid-label-no-false-success")
    memory_ref = "emotion://" + saved["memory"]["memory_id"] + "@1"
    revised = await call("revise_memory", {"target_ref": memory_ref, "changes": {"summary": "已更新的重放正文"}})
    check(revised["decision"] == "revised", "advanced-replay-source-revised")
    stale = await call("remember_emotional_memory", {**replay_args, "emotions": ["场景边界"]})
    check(stale["decision"] == "reject" and stale["reason_code"] == "emotion_memory_unavailable_or_stale"
        and count("emotion_layer_tags") == before_tags, "advanced-replay-stale-version-cannot-attach")
    return {"decision": "PASS", "checks": checks, "count": len(checks)}


async def isolated_probe():
    def deny(*args, **kwargs):
        raise AssertionError("network-forbidden")
    with ExitStack() as guard:
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo"):
            guard.enter_context(patch(name, side_effect=deny))
        return await probe()


class EmotionLayerTransportTests(unittest.TestCase):
    def test_real_registered_tools_and_recall(self):
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="emotion-layer-synthetic-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1", "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000", "STBRAIN_OWNER_ID": "synthetic-owner",
                "STBRAIN_MODEL_ID": "synthetic-model", "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"), "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-emotion-epoch",
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            completed = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_emotion_layer_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(isolated_probe()), ensure_ascii=False))
    else:
        unittest.main()
