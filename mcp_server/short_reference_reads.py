"""Resolve automatic short references for explicit reads, never for writes.

Only identifiers/version numbers are read here. The caller must first perform its
module authorization and then use the normal reader, including all disclosure,
quarantine, lifecycle, auditing and host catalog checks. No reference map is saved.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from runtime.recall_projection import SHORT_REF, PREFIXES, short_ref


class ShortReferenceError(ValueError):
    pass


_MODULES = {
    'emotion': ('emotion_memories', 'memory_id', 'memory_id'),
    'learning': ('learning_items', 'learning_id', 'target_ref'),
    'plan': ('planning_items', 'plan_id', 'plan_ref'),
    'tool-card': ('tool_cards', 'card_id', 'card_id'),
}


def resolve_short_read_fields(*, database, owner_id, model_id, module, fields):
    """Accept an exact short reference in query or the existing identity field."""
    table, column, target = _MODULES[module]
    supplied = {key: value.strip() for key in ('query', target)
                if isinstance(value := fields.get(key), str) and value.strip()}
    aliases = {key: value for key, value in supplied.items() if SHORT_REF.fullmatch(value)}
    if not aliases:
        return fields
    if len(supplied) != 1:
        raise ShortReferenceError('short_reference_requires_one_target')
    source, alias = next(iter(aliases.items()))
    if alias[1] != PREFIXES[module]:
        raise ShortReferenceError('short_reference_module_mismatch')
    try:
        path = Path(database).resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA query_only=ON')
            # Table and column names are internal constants, never caller input.
            matches = [(identity, version) for identity, version in db.execute(
                f'SELECT {column},current_version FROM {table} WHERE owner_id=? AND model_id=?',
                (owner_id, model_id)) if short_ref(f'{module}://{identity}') == alias]
    except (OSError, sqlite3.Error) as exc:
        raise ShortReferenceError('short_reference_lookup_unavailable') from exc
    if not matches:
        raise ShortReferenceError('short_reference_not_found')
    if len(matches) != 1:
        raise ShortReferenceError('short_reference_ambiguous_use_full_reference')
    identity, version = matches[0]
    result = dict(fields)
    if source == 'query':
        result['query'] = ''
    result[target] = (f'{module}://{identity}@{version}'
                      if module in {'learning', 'plan'} else identity)
    if module == 'tool-card' and result.get('view', 'suggestions') == 'suggestions':
        result['view'] = 'card'
    return result
