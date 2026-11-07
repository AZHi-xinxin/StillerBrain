import hashlib
import json
import unittest
from rikkahub_gateway.catalog_diagnostics import mismatch_record


class CatalogDiagnosticsTests(unittest.TestCase):
    def test_added_removed_and_schema_changes_are_distinct_without_names_or_values(self):
        def catalog(items):return {'entries':[{'canonical_name':n,'schema_hash':s} for n,s in items]}
        before=catalog([('personal-server-old','old'),('same-name','before'),('unchanged','s')])
        after=catalog([('new-tool','new'),('same-name','after'),('unchanged','s')])
        text=mismatch_record(before,after);data=json.loads(text)
        self.assertEqual((3,3,1,1,1),tuple(data[k] for k in ['before_count','after_count','added_count','removed_count','schema_changed_count']))
        self.assertNotIn('personal-server',text);self.assertNotIn('same-name',text)
        self.assertEqual([hashlib.sha256(b'new-tool').hexdigest()],data['added_name_sha256'])
        self.assertIsNone(mismatch_record(before,after,['tool_catalog_mismatch']))

    def test_fingerprints_are_bounded_but_counts_are_complete(self):
        after={'entries':[{'canonical_name':str(i),'schema_hash':'s'} for i in range(100)]}
        data=json.loads(mismatch_record({},after))
        self.assertEqual(100,data['added_count']);self.assertEqual(20,len(data['added_name_sha256']))
