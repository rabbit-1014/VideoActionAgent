"""Call accounting preserves delivered text, original failures and cancellation."""
import unittest

from purpose_app import complete_measured


class Ledger:
    def __init__(self):
        self.row = {"state": "cancelled", "model_calls": 1}

    def get(self, identity):
        return dict(self.row)

    def update(self, identity, **changes):
        self.row.update(changes)


class CallAccountingTests(unittest.TestCase):
    def test_generation_and_review_are_separate_without_changing_task_state(self):
        ledger = Ledger()

        class Provider:
            last_call_metrics = None

            def complete(self, system, prompt):
                self.last_call_metrics = {"status": "succeeded", "usage": {"total_tokens": 19}}
                return "unchanged reply"

        provider = Provider()
        for stage in ("generation", "source_review"):
            self.assertEqual(complete_measured(provider, ledger, "job", stage, "", "text"), "unchanged reply")
        self.assertEqual(set(ledger.row["model_measurements"]), {"generation", "source_review"})
        self.assertEqual(ledger.row["model_measurements"]["generation"]["usage"], {"total_tokens": 19})
        self.assertEqual((ledger.row["state"], ledger.row["model_calls"]), ("cancelled", 1))

    def test_failed_call_is_accounted_without_replacing_error_or_claiming_usage(self):
        ledger = Ledger()
        original = TimeoutError("provider timeout")

        class Provider:
            last_call_metrics = None

            def complete(self, system, prompt):
                self.last_call_metrics = {"status": "failed", "usage": None, "error_code": "request_timeout"}
                raise original

        with self.assertRaises(TimeoutError) as caught:
            complete_measured(Provider(), ledger, "job", "generation", "", "text")
        self.assertIs(caught.exception, original)
        self.assertIsNone(ledger.row["model_measurements"]["generation"]["usage"])
        self.assertEqual(ledger.row["state"], "cancelled")
        ledger.update = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
        with self.assertLogs("purpose_app", level="WARNING"), self.assertRaises(TimeoutError) as caught:
            complete_measured(Provider(), ledger, "job", "generation", "", "text")
        self.assertIs(caught.exception, original)


if __name__ == "__main__":
    unittest.main()
