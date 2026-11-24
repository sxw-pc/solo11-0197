from wafer_retest import audit
from wafer_retest.cli import dispatch, parser, read_json
from wafer_retest.db import RuleError, transaction
from wafer_retest.surface_review import review

from tests.ledger_case import FIXTURES, LedgerCase


class SurfaceScanTests(LedgerCase):
    def test_receipt_review_cutoff_and_replay(self):
        self.baseline()
        import_args = parser().parse_args(['--db', ':memory:', 'surface-scan', str(FIXTURES / 'surface-scan.json')])
        with transaction(self.db):
            self.assertFalse(dispatch(self.db, import_args)['duplicate'])
            self.assertTrue(dispatch(self.db, import_args)['duplicate'])
        review_args = parser().parse_args(['--db', ':memory:', 'surface-review', 'W01-01-MICRO', '--min-scratch', '4'])
        result = dispatch(self.db, review_args)
        self.assertEqual((result['decision'], result['scratch_areas']), ('QUARANTINE', [4, 1]))
        self.assertEqual(review(self.db, result['scan'], 5)['decision'], 'CLEAR')
        with self.assertRaisesRegex(RuleError, 'unknown at cutoff'):
            review(self.db, result['scan'], 4, result['receipt'] - 1)
        self.assertEqual(audit.audit(self.db)['decision'], 'MATCH')

    def test_partial_scan_and_overlapping_observations(self):
        self.baseline()
        original = read_json(FIXTURES / 'surface-scan.json')
        partial = dict(original, scan='partial', runs=original['runs'][:3])
        self.action('surface-scan', **partial)
        self.assertEqual(review(self.db, 'partial', 3)['decision'], 'PENDING_SCAN')
        self.assertEqual(review(self.db, 'partial', 2)['decision'], 'QUARANTINE')
        with self.assertRaisesRegex(RuleError, 'overlapping observed runs'):
            self.action('surface-scan', **dict(original, runs=original['runs'] + [[1, 2, 3, 'clean']]))
        with self.assertRaisesRegex(RuleError, 'already names another original'):
            self.action('surface-scan', **dict(partial, runs=original['runs']))
