from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from runtime.planning_memory import PlanningMemoryError, PlanningMemoryStore


OWNER = "owner-a"
MODEL = "model-a"


def calm() -> dict[str, object]:
    return {
        "authorship_confirmed": True,
        "current_state_checked": True,
        "dependencies_checked": True,
        "consequences_reviewed": True,
        "rollback_understood": True,
        "notes": "我已在平静状态下复核这次选择。",
    }


def evidence(label: str = "本轮已实际完成") -> list[dict[str, str]]:
    return [
        {
            "source_kind": "current_conversation",
            "source_ref": "wake://evidence/current",
            "evidence_summary": label,
            "provenance": "verified",
        }
    ]


def content(
    title: str,
    *,
    kind: str = "task",
    track: str = "internal",
    parent_ref: str | None = None,
    dependency_refs: list[str] | None = None,
    presence_mode: str = "relevant",
) -> dict[str, object]:
    return {
        "kind": kind,
        "track": track,
        "title": title,
        "original_text": f"我选择把“{title}”作为自己的计划。",
        "summary": title,
        "reminder": f"记得{title}" if track == "internal" else "",
        "importance": 70,
        "presence_mode": presence_mode,
        "scene_tags": [title, "测试场景"],
        "keywords": [title],
        "start_at": None,
        "due_at": None,
        "timezone": "Asia/Shanghai",
        "review_after": None,
        "allow_coordination_hint": True,
        "parent_ref": parent_ref,
        "dependency_refs": dependency_refs or [],
        "ai_adoption_statement": f"我愿意采纳并维护计划：{title}",
    }


class PlanningMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "planning.sqlite3"
        self.store = PlanningMemoryStore(self.database)
        self.store.ensure_state(owner_id=OWNER, model_id=MODEL)
        self.wake_seq = 0
        self.key_seq = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def key(self, prefix: str = "key") -> str:
        self.key_seq += 1
        return f"{prefix}-{self.key_seq}"

    def propose(self, plan_content: dict[str, object]) -> dict[str, object]:
        self.wake_seq += 1
        return self.store.propose_create(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id=f"wake-{self.wake_seq}",
            wake_seq=self.wake_seq,
            expected_row_version=self.store.status(owner_id=OWNER, model_id=MODEL)[
                "row_version"
            ],
            content=plan_content,
            reason="这是我自己决定采用的计划。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key=self.key("create"),
        )

    def accept(self, pending: dict[str, object]) -> dict[str, object]:
        self.wake_seq += 1
        wake_id = f"wake-{self.wake_seq}"
        presented = self.store.present_pending_candidates(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id=wake_id,
            wake_seq=self.wake_seq,
        )
        candidate = next(
            item for item in presented if item["candidate_id"] == pending["candidate_id"]
        )
        self.assertTrue(candidate["fully_presented"])
        return self.store.review_change(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id=wake_id,
            wake_seq=self.wake_seq,
            expected_row_version=self.store.status(owner_id=OWNER, model_id=MODEL)[
                "row_version"
            ],
            candidate_id=str(candidate["candidate_id"]),
            expected_candidate_version=int(candidate["candidate_version"]),
            expected_candidate_hash=str(candidate["candidate_hash"]),
            expected_base_version=int(candidate["base_version"]),
            decision="accept",
            correctness_assessment="内容、差异、来源和后果都符合我的判断。",
            calm_check=calm(),
            reason="我在新的真实唤醒中独立接受。",
            ai_confirmation=True,
        )

    def create(self, plan_content: dict[str, object]) -> dict[str, object]:
        return self.accept(self.propose(plan_content))

    def record(
        self,
        accepted: dict[str, object],
        event_type: str,
        *,
        event_evidence: list[dict[str, str]] | None = None,
        wake_id: str | None = None,
    ) -> dict[str, object]:
        self.wake_seq += 1
        return self.store.record_event(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id=wake_id or f"wake-{self.wake_seq}",
            wake_seq=self.wake_seq,
            expected_row_version=self.store.status(owner_id=OWNER, model_id=MODEL)[
                "row_version"
            ],
            plan_id=str(accepted["plan_ref"]).split("//", 1)[1].split("@", 1)[0],
            expected_plan_version=int(str(accepted["plan_ref"]).rsplit("@", 1)[1]),
            event_type=event_type,
            reason=f"记录事件：{event_type}",
            evidence=event_evidence or [],
            ai_confirmation=True,
            idempotency_key=self.key("event"),
        )

    def test_schema_and_empty_status(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas" / "planning-memory.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("urn:stiller-brain:planning-memory:0.1", schema["$id"])
        status = self.store.status(owner_id=OWNER, model_id=MODEL)
        self.assertEqual(0, status["row_version"])
        self.assertEqual(0, status["counts"]["plans"])

    def test_legacy_candidate_accepts_exact_confirmation_without_later_wake(self) -> None:
        pending = self.propose(content("完成规划脑测试"))
        same_wake = self.store.present_pending_candidates(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id=f"wake-{self.wake_seq}",
            wake_seq=self.wake_seq,
        )[0]
        self.assertTrue(same_wake["fully_presented"])
        self.assertFalse(same_wake["review_requires_later_wake"])
        accepted = self.store.review_change(
                owner_id=OWNER,
                model_id=MODEL,
                wake_id=f"wake-{self.wake_seq}",
                wake_seq=self.wake_seq,
                expected_row_version=int(pending["planning_row_version"]),
                candidate_id=str(pending["candidate_id"]),
                expected_candidate_version=int(pending["candidate_version"]),
                expected_candidate_hash=str(pending["candidate_hash"]),
                expected_base_version=int(pending["base_version"]),
                decision="accept",
                correctness_assessment="我确认内容正确。",
                reason="接受。",
                ai_confirmation=True,
            )
        exact = self.store.recall(
            owner_id=OWNER, model_id=MODEL, plan_ref=str(accepted["plan_ref"])
        )
        self.assertEqual("完成规划脑测试", exact["plans"][0]["content"]["title"])
        with self.assertRaisesRegex(PlanningMemoryError, "plan_not_found"):
            self.store.recall(
                owner_id="other-owner",
                model_id=MODEL,
                plan_ref=str(accepted["plan_ref"]),
            )

    def test_cas_idempotency_credentials_and_persistent_limit(self) -> None:
        row = self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"]
        kwargs = dict(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-idempotent",
            wake_seq=1,
            expected_row_version=row,
            content=content("幂等计划"),
            reason="由我建立。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key="same-key",
        )
        first = self.store.propose_create(**kwargs)
        second = self.store.propose_create(**kwargs)
        self.assertEqual(first, second)
        with self.assertRaisesRegex(PlanningMemoryError, "planning_row_version_conflict"):
            self.store.propose_create(
                **{**kwargs, "idempotency_key": "wrong-cas", "content": content("过期版本")}
            )
        with self.assertRaisesRegex(PlanningMemoryError, "credential_content_rejected"):
            self.store.propose_create(
                **{
                    **kwargs,
                    "expected_row_version": int(first["planning_row_version"]),
                    "idempotency_key": "secret",
                    "reason": "token=abcdefghijklmnop",
                }
            )
        self.wake_seq = 1
        self.accept(first)
        self.create(content("常驻一", presence_mode="persistent"))
        self.create(content("常驻二", presence_mode="persistent"))
        with self.assertRaisesRegex(PlanningMemoryError, "persistent_plan_limit"):
            self.propose(content("常驻三", presence_mode="persistent"))

    def test_graph_is_acyclic_and_hierarchy_is_checked(self) -> None:
        vision = self.create(content("长期愿景", kind="vision"))
        goal = self.create(
            content("阶段目标", kind="goal", parent_ref=str(vision["plan_ref"]))
        )
        vision_id = str(vision["plan_ref"]).split("//", 1)[1].split("@", 1)[0]
        with self.assertRaisesRegex(PlanningMemoryError, "planning_graph_cycle"):
            self.store.propose_revision(
                owner_id=OWNER,
                model_id=MODEL,
                wake_id="wake-cycle",
                wake_seq=99,
                expected_row_version=self.store.status(owner_id=OWNER, model_id=MODEL)[
                    "row_version"
                ],
                plan_id=vision_id,
                expected_plan_version=1,
                intent="revise",
                changes={"dependency_refs": [goal["plan_ref"]]},
                reason="尝试引入环。",
                calm_check=calm(),
                ai_confirmation=True,
                idempotency_key=self.key("cycle"),
            )

    def test_evidence_gates_and_two_cross_wake_defers_pause(self) -> None:
        task = self.create(content("核验功能"))
        with self.assertRaisesRegex(PlanningMemoryError, "evidence_required"):
            self.record(task, "progress")
        progressed = self.record(task, "progress", event_evidence=evidence())
        self.assertEqual("active", progressed["plan_state"]["state"])
        completed = self.record(task, "complete", event_evidence=evidence("验收已通过"))
        self.assertEqual("completed", completed["plan_state"]["state"])

        delayed = self.create(content("稍后复核"))
        first = self.record(delayed, "defer_review", wake_id="same-defer-wake")
        second = self.record(delayed, "defer_review", wake_id="same-defer-wake")
        self.assertEqual("active", first["plan_state"]["state"])
        self.assertEqual("active", second["plan_state"]["state"])
        third = self.record(delayed, "defer_review", wake_id="different-defer-wake")
        self.assertEqual("paused", third["plan_state"]["state"])

    def test_dependency_next_action_is_advisory_and_injection_is_bounded(self) -> None:
        prerequisite = self.create(content("先完成甲"))
        dependent = self.create(
            content("再完成乙", dependency_refs=[str(prerequisite["plan_ref"])])
        )
        before_version = self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"]
        first = self.store.next_action(owner_id=OWNER, model_id=MODEL)
        self.assertEqual(prerequisite["plan_ref"], first["plan_ref"])
        self.assertTrue(first["advisory_only"])
        self.assertFalse(first["mutates_plan"])
        self.assertEqual(
            before_version, self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"]
        )
        self.record(prerequisite, "complete", event_evidence=evidence())
        second = self.store.next_action(owner_id=OWNER, model_id=MODEL)
        self.assertEqual(dependent["plan_ref"], second["plan_ref"])

        self.create(content("会话顶层", presence_mode="session_start"))
        self.create(content("测试场景协作一", track="relational"))
        self.create(content("测试场景协作二", track="relational"))
        row_before = self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"]
        preview = self.store.build_injection(
            owner_id=OWNER,
            model_id=MODEL,
            query="测试场景协作",
            session_start=True,
            limit=3,
        )
        self.assertLessEqual(len(preview["envelopes"]), 3)
        self.assertLessEqual(
            sum(1 for item in preview["envelopes"] if item["content"]["summary"] == "会话顶层"),
            1,
        )
        self.assertTrue(all(item["presentation"] == "summary_only" for item in preview["envelopes"]))
        self.assertEqual(
            row_before, self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"]
        )

    def test_abandoned_plan_can_be_recalled_and_revived(self) -> None:
        task = self.create(content("可以改变主意的计划"))
        plan_id = str(task["plan_ref"]).split("//", 1)[1].split("@", 1)[0]
        abandon = self.store.propose_revision(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-abandon",
            wake_seq=100,
            expected_row_version=self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"],
            plan_id=plan_id,
            expected_plan_version=1,
            intent="abandon",
            reason="我现在决定停止。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key=self.key("abandon"),
        )
        self.wake_seq = 100
        abandoned = self.accept(abandon)
        self.assertEqual("abandoned", abandoned["plan_state"]["state"])
        exact = self.store.recall(
            owner_id=OWNER,
            model_id=MODEL,
            plan_ref=str(task["plan_ref"]),
            include_terminal=True,
        )
        self.assertEqual("abandoned", exact["plans"][0]["state_projection"]["state"])
        revive = self.store.propose_revision(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-revive",
            wake_seq=200,
            expected_row_version=self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"],
            plan_id=plan_id,
            expected_plan_version=1,
            intent="revive",
            reason="我现在愿意重新开始。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key=self.key("revive"),
        )
        self.wake_seq = 200
        revived = self.accept(revive)
        self.assertEqual("active", revived["plan_state"]["state"])


if __name__ == "__main__":
    unittest.main()
