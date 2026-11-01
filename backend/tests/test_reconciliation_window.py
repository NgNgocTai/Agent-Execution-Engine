from datetime import timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from core.clock import utcnow
from core.config import settings
from controllers.operations import require_dispatch_window_closed, Reconciliation


class ReconciliationWindowTest(unittest.TestCase):
    def test_unknown_thread_cannot_release_guard_before_its_dispatch_deadline(self):
        approval = SimpleNamespace(status="unknown", resolved_at=utcnow(), expires_at=utcnow() + timedelta(minutes=5))
        with self.assertRaises(HTTPException) as raised: require_dispatch_window_closed(approval)
        self.assertEqual(raised.exception.status_code, 409)
        approval.resolved_at = utcnow() - timedelta(seconds=settings.ssh_total_timeout_seconds + 2)
        require_dispatch_window_closed(approval)
    def test_blank_evidence_is_rejected(self):
        with self.assertRaises(ValueError): Reconciliation(outcome="verified_failed", evidence=" " * 30)
