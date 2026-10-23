"""Shared execution envelope and failure semantics."""

from types import SimpleNamespace
import unittest

from ralph import runner
from ralph.cli import resolve_archaeology
from ralph.engine import Output


class RunnerTest(unittest.TestCase):
    def fixture(self, output=None, error=None):
        snapshot = SimpleNamespace(base_sha='base', head_sha='head',
                                   merge_base='merge', diff_base_sha='base')
        case = SimpleNamespace(snapshot=snapshot)
        runtime = SimpleNamespace(budget=None, ppq_budget=None, attempt={},
                                  metrics=lambda: {'requests': 0},
                                  check_current=lambda: None)

        def run(case, runtime):
            runtime.attempt['entered'] = True
            if error is not None:
                raise error
            return output or Output('review', {'custom': True}, {}, 3)

        selected = SimpleNamespace(identity={'name': 'test'}, run=run)
        return case, runtime, selected

    def test_success_preserves_exact_diff_base_and_native_schema(self):
        case, runtime, selected = self.fixture()
        result = runner.run_review(case, runtime, selected)
        self.assertEqual(result['base_sha'], 'base')
        self.assertEqual(result['diff_base_sha'], 'base')
        self.assertEqual(result['merge_base'], 'merge')
        self.assertEqual(result['output']['schema_version'], 3)
        self.assertEqual(result['run']['status'], 'completed')
        self.assertTrue(result['run']['review_id'])
        self.assertTrue(result['run']['attempt']['entered'])

    def test_original_exception_and_attempt_survive(self):
        error = ConnectionError('original transport failure')
        case, runtime, selected = self.fixture(error=error)
        with self.assertRaises(ConnectionError) as raised:
            runner.run_review(case, runtime, selected)
        self.assertIs(raised.exception, error)
        failure = runner.failure_result(case, runtime, selected, error)
        self.assertTrue(failure['run']['attempt']['entered'])
        self.assertEqual(failure['error']['type'], 'ConnectionError')

    def test_budget_id_is_preserved(self):
        case, runtime, selected = self.fixture()
        runtime.budget = SimpleNamespace(review_id='existing-review', summary=lambda: {'usd': 1})
        result = runner.run_review(case, runtime, selected)
        self.assertEqual(result['run']['review_id'], 'existing-review')
        self.assertEqual(result['run']['budget'], {'usd': 1})

    def test_non_json_and_nonfinite_output_rejected(self):
        for data in ({'set': {1}}, {'number': float('nan')}):
            with self.subTest(data=data):
                case, runtime, selected = self.fixture(Output('review', data, {}))
                with self.assertRaises((TypeError, ValueError)):
                    runner.run_review(case, runtime, selected)

    def test_success_preserves_budget_accounting_failure(self):
        error = RuntimeError('accounting unavailable')
        case, runtime, selected = self.fixture()

        def summary():
            raise error

        runtime.budget = SimpleNamespace(review_id='existing-review', summary=summary)
        with self.assertRaises(RuntimeError) as raised:
            runner.run_review(case, runtime, selected)
        self.assertIs(raised.exception, error)
        failure = runner.failure_result(case, runtime, selected, error)
        self.assertEqual(failure['error']['message'], 'accounting unavailable')
        self.assertIsNone(failure['run']['budget'])

    def test_archaeology_resolution(self):
        source = {'kind': 'pull_request'}
        self.assertFalse(resolve_archaeology('auto', None, None)[0])
        self.assertFalse(resolve_archaeology('auto', source, None)[0])
        self.assertTrue(resolve_archaeology('auto', source, {})[0])
        self.assertFalse(resolve_archaeology('off', source, {})[0])
        for source, evidence in ((None, None), (source, None), (None, {})):
            with self.assertRaises(ValueError):
                resolve_archaeology('on', source, evidence)


if __name__ == '__main__':
    unittest.main()
