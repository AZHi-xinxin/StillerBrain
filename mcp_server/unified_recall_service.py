"""Owner-scoped, summary-only ordinary recall with change-detecting pagination.

This adapter reuses the existing explicit matchers, not automatic wake recall.
It does not initialize stores, save queries, mint wakes, or cache memory bodies.
Same-file modules share a read transaction; separate files retain independent
snapshots, explicitly identified in the response. Cursors contain only opaque
hashes and an offset, authenticated with a process-local key (restart -> retry).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from runtime.credential_guard import contains_credential_or_secret
from runtime.lexical_retrieval import explicit_alias_match
from runtime.tool_guidance import _checked_version_content, _scene_score


MODULES = ('emotional_memory', 'learning_memory', 'planning_memory', 'tool_guidance')
_MODULE_NAMES = {'emotional_memory': 'emotional_memory_module_two',
                 'learning_memory': 'learning_memory_module_three',
                 'planning_memory': 'planning_memory_module_five',
                 'tool_guidance': 'tool_guidance_module'}
_CURSOR_KEY = secrets.token_bytes(32)
_RESTRICTED_TITLE = '受限记忆'
_RESTRICTED_SUMMARY = '目录保留版本引用；原文请通过对应模块的精确读取及披露规则查看。'


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _encode(value: Any) -> str:
    raw = _canonical(value)
    return base64.urlsafe_b64encode(raw + hmac.digest(_CURSOR_KEY, raw, 'sha256')).decode().rstrip('=')


def _decode(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError('cursor_invalid')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        payload, signature = raw[:-32], raw[-32:]
        if len(signature) != 32 or not hmac.compare_digest(signature, hmac.digest(_CURSOR_KEY, payload, 'sha256')):
            raise ValueError
        decoded = json.loads(payload)
        if (not isinstance(decoded, dict) or set(decoded) != {'v', 'binding', 'snapshot', 'offset'}
                or decoded['v'] != 1 or type(decoded['offset']) is not int or decoded['offset'] < 0):
            raise ValueError
        return decoded
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('cursor_invalid') from exc


@contextmanager
def _read_connection(path: Path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


class UnifiedRecallAccessService:
    def __init__(self, *, services: dict[str, Any], owner_id: str, model_id: str,
                 onboarding: Any, simple: bool):
        self.services = dict(services)
        self.owner_id, self.model_id = owner_id, model_id
        self.onboarding, self.simple = onboarding, simple
        if not owner_id or not model_id or any(
                service is not None and (service.owner_id, service.model_id) != (owner_id, model_id)
                for service in self.services.values()):
            raise ValueError('unified_recall_identity_mismatch')

    @staticmethod
    def _reject(code: str) -> dict[str, Any]:
        return {'decision': 'reject', 'reason_code': code, 'state_changed': False,
                'items': [], 'next_cursor': None,
                'message': ('目录或查询结果已变化，请省略 cursor 重新查询；原记录保持原样。'
                            if code == 'cursor_stale' else
                            '请使用有效的查询参数或本次返回的 cursor；服务重启后可省略 cursor 重新查询。')}

    def recall(self, *, query: str = '', module: str | None = None,
               limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        if (not isinstance(query, str) or len(query) > 4000 or module not in (*MODULES, None)
                or type(limit) is not int or not 1 <= limit <= 50):
            return self._reject('invalid_recall_arguments')
        if contains_credential_or_secret(query):
            return self._reject('credential_or_secret_detected')
        selected = (module,) if module is not None else MODULES
        query = query.strip()
        binding = _digest([self.owner_id, self.model_id, selected, query])
        previous = None
        if cursor is not None:
            try:
                previous = _decode(cursor)
            except ValueError:
                return self._reject('cursor_invalid')
            if previous['binding'] != binding:
                return self._reject('cursor_query_or_identity_mismatch')
        items, errors, stamps, groups = [], [], {}, {}
        with ExitStack() as stack:
            connections = {}
            for name in selected:
                service = self.services.get(name)
                if service is None:
                    errors.append({'module': name, 'reason_code': 'module_unavailable'})
                    continue
                try:
                    if not self.simple and self.onboarding.authorize_other_module_write(
                            owner_id=self.owner_id, model_id=self.model_id,
                            module_name=_MODULE_NAMES[name]).get('decision') != 'allowed':
                        errors.append({'module': name, 'reason_code': 'module_read_unavailable'})
                        continue
                    path = Path(service.store.database).resolve(strict=True)
                    if not path.is_file():
                        raise ValueError('module_unavailable')
                    if path not in connections:
                        connections[path] = stack.enter_context(_read_connection(path))
                    groups[name] = list(connections).index(path)
                    found, fingerprint = self._module(name, service.store, connections[path], query)
                    # Recheck projected data, including old records predating the
                    # credential guard. Errors never echo stored content or paths.
                    safe = []
                    for item in found:
                        if contains_credential_or_secret(item):
                            error = {'module': name, 'reason_code': 'protected_content_withheld'}
                            if error not in errors:
                                errors.append(error)
                            continue
                        safe.append(item)
                    stamps[name] = _digest([fingerprint, safe])
                    items.extend(safe)
                except Exception:
                    errors.append({'module': name, 'reason_code': 'module_read_failed'})
        # Stable grouping, never compare unrelated per-brain scores as probabilities.
        items.sort(key=lambda item: (MODULES.index(item['module']), item['ref']))
        errors.sort(key=lambda item: (item['module'], item['reason_code']))
        snapshot = _digest([stamps, errors])
        if previous is not None and previous['snapshot'] != snapshot:
            return self._reject('cursor_stale')
        offset = previous['offset'] if previous else 0
        if offset > len(items):
            return self._reject('cursor_invalid')
        page = items[offset:offset + limit]
        end = offset + len(page)
        has_more = end < len(items)
        # Healthy modules remain pageable while the same failures are disclosed.
        # Recovery/failure or content changes invalidate the old result snapshot.
        next_cursor = (_encode({'v': 1, 'binding': binding, 'snapshot': snapshot, 'offset': end})
                       if has_more else None)
        return {'decision': 'recalled' if query else 'directory', 'view': 'recall',
                'mode': 'search' if query else 'directory', 'items': page,
                'searched_modules': [name for name in selected if name in stamps],
                'requested_modules': list(selected), 'errors': errors, 'partial': bool(errors),
                'returned_count': len(page), 'matched_count': len(items), 'offset': offset,
                'limit': limit, 'has_more': has_more, 'truncated': has_more,
                'next_cursor': next_cursor, 'exhaustive': not errors and offset == 0 and not has_more,
                'snapshot_scope': ('shared_database' if len(set(groups.values())) <= 1 else 'per_database'),
                'ordering': 'module_then_versioned_reference', 'summary_only': True,
                'state_changed': False, 'write_context_created': False,
                'guidance': '按 detail_lookup 读取原文与历史；查询结果是线索，不是事实核验。续页原样保留 query/module，带返回的 cursor。数据变化时重新查询。'}

    @staticmethod
    def _entry(module: str, ref: str, title: str, summary: str, lifecycle: str,
               tool: str, arguments: dict[str, Any], *, restricted: bool = False,
               hint: dict[str, Any] | None = None) -> dict[str, Any]:
        result = {'module': module, 'ref': ref, 'title': _RESTRICTED_TITLE if restricted else title,
                  'summary': _RESTRICTED_SUMMARY if restricted else summary, 'lifecycle': lifecycle,
                  'content_stub': restricted, 'detail_lookup': {'tool': tool, 'arguments': arguments},
                  'permission_authority': 'none'}
        if hint is not None:
            result.update({'retrieval_match': 'lexical_alias_candidate', 'candidate_only': True,
                           'retrieval_note': '词句别名候选；只提供查找线索，不代表事件已经发生或已经核验。'})
        return result

    def _module(self, name: str, store: Any, connection: sqlite3.Connection,
                query: str) -> tuple[list[dict[str, Any]], Any]:
        identity = {'owner_id': self.owner_id, 'model_id': self.model_id}
        values = (self.owner_id, self.model_id)
        result = []
        if name == 'emotional_memory':
            # Deliberately excludes emotion_ephemeral (including expired rows),
            # pins and injection caches. Their retention is not a directory write.
            rows = connection.execute("SELECT * FROM emotion_memories WHERE owner_id=? AND model_id=? AND lifecycle IN ('active','archived') ORDER BY memory_id", values).fetchall()
            hints, eligible = set(), None
            if query:
                aliases = []
                scored = store._scored_rows(connection, **identity, query=query,
                                            include_archived=True, lexical_candidates=aliases)
                eligible = {row['memory_id'] for row, *_ in scored}
                hints = {row['memory_id'] for row, *_ in aliases} - eligible
                eligible |= hints
            for row in rows:
                if eligible is not None and row['memory_id'] not in eligible:
                    continue
                public = store._public_memory(row, include_original=False)
                ref = f"emotion://{row['memory_id']}@{row['current_version']}"
                result.append(self._entry(name, ref, '情感与经历', public['summary'], row['lifecycle'],
                    'recall_emotional_memory', {'memory_id': row['memory_id'], 'include_originals': True},
                    restricted=row['sensitivity'] in {'intimate', 'restricted'},
                    hint={} if row['memory_id'] in hints else None))
            return result, [dict(row) for row in rows]
        if name == 'learning_memory':
            # Match inventory(include_archived=True, include_pending=True).
            # Pending memory cards are readable; semantic change candidates are
            # a separate table and quarantined cards remain excluded here.
            rows = connection.execute("SELECT * FROM learning_items WHERE owner_id=? AND model_id=? AND lifecycle IN ('active','archived','superseded','pending_review') ORDER BY learning_id", values).fetchall()
            lineage = store._accepted_integration_lineage(connection, **identity) if query else {}
            for row in rows:
                content = json.loads(row['current_json'])
                hint = None
                if query:
                    score, _ = store._manual_search_match(content, query)
                    lineage_score, refs = store._integration_query_matches(
                        learning_id=row['learning_id'], lineage=lineage, query=query, manual_search=True)
                    if refs:
                        score = max(score, min(1.0, lineage_score + 0.02))
                    if score < 0.15:
                        hint = explicit_alias_match(query, [content['title'], content['summary'],
                            *content.get('scene_tags', []), *content.get('keywords', [])])
                        if hint is None:
                            continue
                ref = f"learning://{row['learning_id']}@{row['current_version']}"
                result.append(self._entry(name, ref, content['title'], content['summary'], row['lifecycle'],
                    'recall_learning_memory', {'target_ref': ref, 'include_archived': True, 'include_versions': True,
                                              'include_pending': row['lifecycle'] == 'pending_review'},
                    restricted=content.get('sensitivity') in {'intimate', 'restricted'}, hint=hint))
            return result, [[dict(row) for row in rows], lineage]
        if name == 'planning_memory':
            rows = connection.execute("SELECT * FROM planning_items WHERE owner_id=? AND model_id=? AND recall_lifecycle!='quarantined' ORDER BY plan_id", values).fetchall()
            fingerprints = []
            for row in rows:
                public = store._public_plan(connection, **identity, item=row)
                content, hint = public['content'], None
                fingerprints.append([dict(row), public['content_hash'], public['event_seq']])
                if query and store._semantic_score(content, query) < 0.15:
                    hint = explicit_alias_match(query, [content['title'], content['summary'],
                        *content.get('scene_tags', []), *content.get('keywords', [])])
                    if hint is None:
                        continue
                result.append(self._entry(name, public['plan_ref'], content['title'], content['summary'],
                    public['state_projection']['state'], 'recall_planning_memory',
                    {'plan_ref': public['plan_ref'], 'include_terminal': True, 'include_history': True}, hint=hint))
            return result, fingerprints
        pairs = store._candidate_rows(connection, **identity)
        for card, version in pairs:
            content, hint = _checked_version_content(version), None
            if query and _scene_score(query, content) < 0.5:
                hint = explicit_alias_match(query, [content['purpose'], content.get('reminder', ''),
                    *content['keywords'], *content['aliases'], *content['scenario_tags'],
                    *content.get('scenario_examples', []), *content.get('use_when', [])])
                if hint is None:
                    continue
            ref = f"tool-card://{card['card_id']}@{version['version']}"
            result.append(self._entry(name, ref, content['display_label'],
                content.get('reminder') or content['purpose'], card['lifecycle'],
                'recall_tool_guidance', {'card_id': ref, 'view': 'card', 'include_stale': True}, hint=hint))
        return result, [[dict(card), version['content_hash']] for card, version in pairs]
