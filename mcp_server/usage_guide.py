"""Static, credential-free usage help; reading help never opens a write context."""
from __future__ import annotations
from typing import Any


def usage_guide() -> dict[str, Any]:
    return {
        "contract_version": "daily-memory/1", "state_changed": False,
        "daily_memory": {
            "tool": "remember_memory", "required": ["module", "content"],
            "modules": ["emotional_memory", "learning_memory", "planning_memory"],
            "instruction": "普通新增仅需 module 和真实 content，一次调用；不用先 open、手填版本或另轮审核。title/summary 等可选。",
            "examples": [
                {"module": "learning_memory", "content": "这里填写要保存的知识或经验。"},
                {"module": "planning_memory", "content": "明天检查实验结果。"},
            ],
            "result": "真实 stored 回执才表示已保存，reject 表示拒绝；回执未知或不完整时先核查，不自动重试。",
            "uncertainty": "默认 reported 表示转述，不代表已独立核实。",
            "plan_effect": "普通计划成为 active 记录，不授权或自动执行外部操作。",
        },
        "read": "本帮助属于 StillerBrain（ST），不代表其他 MCP。压缩、重启或新窗口后，如需主动找回 ST 记忆：情感与人际经历用 recall_emotional_memory；知识与方法用 recall_learning_memory；计划与承诺用 recall_planning_memory；工具使用经验用 recall_tool_guidance；活动自我与归档用 query_self_model。使用当前工具列表中 ST 对应的完整名称；无需每轮必读。学习全量目录仅用 recall_learning_memory(view='inventory')；其余按 query 或精确引用查询。零命中不等于库为空。",
        "ordinary_revision": {
            "tool": "revise_memory", "required": ["target_ref", "changes"],
            "instruction": "使用已读的精确版本引用，只改下列字段；不改原文、可信状态或权限。模块行版本由宿主处理，冲突后先读回，不自动换最新版。",
            "allowed_fields": {
                "emotional_memory": ["summary", "keywords", "entities", "importance"],
                "learning_memory": ["title", "summary", "domain", "keywords", "entities", "importance"],
                "planning_memory": ["title", "summary", "keywords", "importance"],
            },
        },
        "plan_progress": {
            "tool": "advance_plan", "required": ["target_ref", "expected_event_seq", "event_type", "note"],
            "instruction": "使用查询返回的目标版本与 event_seq；完成等操作仍需真实证据，不捏造，不手填模块行版本。",
        },
        "advanced": "stbrain_open 默认只给核心修改及高级操作的上下文摘要与版本，不是读全库；说明用 view='manual'、module=对应模块。模块一候选全文用 stbrain_open(view='review') 分页，page 默认0，续页带 expected_material_hash；全部页齐才形成展示证明。核心自改仍保留三个真实唤醒：提交候选、后来独立审核、再后来激活。置顶及高级修订/整合按各自流程；普通新增不套此流程。",
        "transport": "使用 ST 无需 Shell 或 workspace，也无需从文件提取工具结果。正常网关由宿主绑定，不编造或复用 execution_ref。非网关写入须人类独立授权的 direct 上下文；其有效 scope 内也可用 remember_memory。",
        "privacy": "不保存令牌或密码，不回显授权内容；本帮助不含私人记忆。",
    }
