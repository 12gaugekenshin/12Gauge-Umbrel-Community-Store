import unittest

from app.verification import block_hash, compact_target, confirmation_status, verify_proof


GENESIS_HEADER = (
    "01000000" + "00" * 32
    + "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a"
    + "29ab5f49ffff001d1dac2b7c"
)
GENESIS_BLOCK = GENESIS_HEADER + "00"


class VerificationTests(unittest.TestCase):
    def test_genesis_proof_and_hash(self):
        proof = verify_proof(GENESIS_BLOCK)
        self.assertTrue(proof["proofValid"])
        self.assertEqual(
            proof["hash"], "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
        )
        self.assertEqual(block_hash(GENESIS_BLOCK), proof["hash"])

    def test_invalid_header_is_not_proof(self):
        proof = verify_proof(GENESIS_HEADER[:-8] + "00000000" + "00")
        self.assertFalse(proof["proofValid"])

    def test_target_and_states(self):
        self.assertGreater(compact_target(0x1D00FFFF), 0)
        self.assertEqual(confirmation_status(None), "candidate")
        self.assertEqual(confirmation_status(-1), "orphaned")
        self.assertEqual(confirmation_status(1), "confirming")
        self.assertEqual(confirmation_status(6), "confirmed")
        self.assertEqual(confirmation_status(100), "mature")


if __name__ == "__main__":
    unittest.main()
