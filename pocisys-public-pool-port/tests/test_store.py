import os
import tempfile
import unittest

from app.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.store = Store(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_candidate_transitions_are_persisted_and_journaled(self):
        item = {
            "upstream_id": 1, "height": 900000, "miner_address": "bc1qtest",
            "worker": "garage", "session_id": "12345678", "block_hash": "00" * 32,
            "detected_at": "now", "proof_valid": 1, "bits": "17000000",
            "status": "confirming", "confirmations": 1, "coinbase_txid": "11" * 32,
            "last_checked": 1, "error": None,
        }
        self.store.candidate(item)
        item.update(status="confirmed", confirmations=6, last_checked=2)
        self.store.candidate(item)
        self.assertEqual(self.store.candidates()[0]["confirmations"], 6)
        self.assertEqual(len(self.store.events()), 2)


if __name__ == "__main__":
    unittest.main()
