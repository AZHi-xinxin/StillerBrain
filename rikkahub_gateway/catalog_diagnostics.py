"""Catalogue mismatch evidence: counts and name fingerprints, never schemas or values."""
import hashlib
import json


def mismatch_record(before, after, protected_values=()):
    def entries(catalog):
        return {item['canonical_name']:item['schema_hash'] for item in catalog.get('entries',[])}
    a,b=entries(before),entries(after)
    added,removed=set(b)-set(a),set(a)-set(b)
    changed={name for name in set(a)&set(b) if a[name]!=b[name]}
    fingerprint=lambda names:sorted(hashlib.sha256(name.encode()).hexdigest() for name in names)[:20]
    result={'event':'tool_catalog_mismatch','before_count':len(a),'after_count':len(b),
            'added_count':len(added),'removed_count':len(removed),'schema_changed_count':len(changed),
            'added_name_sha256':fingerprint(added),'removed_name_sha256':fingerprint(removed),
            'changed_name_sha256':fingerprint(changed)}
    encoded=json.dumps(result,sort_keys=True,separators=(',',':'))
    return None if any(value and value in encoded for value in protected_values) else encoded
