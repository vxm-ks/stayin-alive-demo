from __future__ import annotations

import unittest

from stage2.completion_quality_policy import (
    MAX_BLOCK_ATTEMPTS,
    MAX_BLOCK_RETRIES,
    candidate_gate_action,
)


class CompletionQualityPolicyTests(unittest.TestCase):
    def test_policy_retries_twice_then_soft_accepts_nonempty_candidate(self):
        self.assertEqual(MAX_BLOCK_RETRIES, 2)
        self.assertEqual(MAX_BLOCK_ATTEMPTS, 3)
        self.assertEqual(
            candidate_gate_action(
                attempt_number=1,
                total_attempts=3,
                has_notes=True,
                melody_ok=False,
            ),
            "retry",
        )
        self.assertEqual(
            candidate_gate_action(
                attempt_number=2,
                total_attempts=3,
                has_notes=True,
                melody_ok=False,
            ),
            "retry",
        )
        self.assertEqual(
            candidate_gate_action(
                attempt_number=3,
                total_attempts=3,
                has_notes=True,
                melody_ok=False,
            ),
            "soft_accept",
        )

    def test_valid_candidate_passes_immediately(self):
        self.assertEqual(
            candidate_gate_action(
                attempt_number=1,
                total_attempts=3,
                has_notes=True,
                melody_ok=True,
            ),
            "accept",
        )

    def test_all_silent_candidates_remain_structurally_unusable(self):
        self.assertEqual(
            candidate_gate_action(
                attempt_number=3,
                total_attempts=3,
                has_notes=False,
                melody_ok=False,
            ),
            "exhausted",
        )


if __name__ == "__main__":
    unittest.main()
