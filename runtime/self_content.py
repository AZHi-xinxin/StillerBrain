"""Lossless views of AI-authored content. Storage keeps the submitted JSON intact."""
from collections.abc import Mapping
import json


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def parts(content):
    return dict(content) if isinstance(content, Mapping) else {'active_identity_capsule': content}


def facet_map(value):
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        return {str(i + 1): item for i, item in enumerate(value)}
    return {} if value is None else {'facets': value}


def object_view(value):
    return dict(value) if isinstance(value, Mapping) else {'text': value}


def stable_view(content, facets=None):
    source = parts(content)
    # Unknown author fields remain visible; framework metadata stays in exact reads.
    result = {k: v for k, v in source.items()
              if k not in {'schema_version', 'anchor_references', 'facet_recall', 'facets'}}
    result.setdefault('boot_anchor', {})
    result.setdefault('active_identity_capsule', {})
    result['facets'] = facets or {}
    return result
