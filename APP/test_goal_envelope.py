"""The model writes content; the runtime owns the immutable request envelope."""
import json
import unittest

from goal_strategy import StrategyError, validate_solution


class GoalEnvelopeTests(unittest.TestCase):
    def test_provider_selection_is_frozen_and_not_model_chosen(self):
        import copy
        from bridge_contract import digest
        from goal_native import policy, validate_policy
        selected = policy()
        validate_policy(selected)
        invalid = copy.deepcopy(selected)
        invalid['generation_provider']['model'] = 'some-unconfigured-model'
        invalid['sha256'] = digest({k:v for k,v in invalid.items() if k != 'sha256'})
        with self.assertRaises(ValueError):
            validate_policy(invalid)
        legacy = {k:v for k,v in selected.items() if k not in {'generation_provider','sha256'}}
        legacy['version'] = 8
        legacy['sha256'] = digest(legacy)
        validate_policy(legacy)

    def content(self):
        return {'schema_version': 2, 'goal': '解释关联', 'success_criteria': ['说明关联条件'],
                'proposed_deliverable': '简短解释', 'status': 'ready', 'gaps': [],
                'markdown': '先比较键，再比较对应值。', 'tool_calls': []}

    def validate(self, value, version):
        return validate_solution(json.dumps(value, ensure_ascii=False), request='原始请求',
            source_digest='a'*64, segment_count=3, available_tools=(), response_version=version)

    def test_content_only_wire_is_bound_to_authoritative_identity_and_rejects_injected_fields(self):
        result = self.validate(self.content(), 2)
        self.assertEqual(result['user_request'], '原始请求')
        self.assertEqual(result['source_digest'], 'a'*64)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['markdown'], self.content()['markdown'])
        for extra in ({'user_request': '偷换目标'}, {'source_digest': 'b'*64}):
            with self.assertRaises(StrategyError):
                self.validate({**self.content(), **extra}, 2)

    def test_new_content_wire_keeps_protocol_version_with_runtime_only(self):
        value = {k:v for k,v in self.content().items() if k != 'schema_version'}
        result = self.validate(value, 3)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['markdown'], value['markdown'])
        with self.assertRaises(StrategyError):
            self.validate(value, 2)
        with self.assertRaises(StrategyError):
            self.validate({**value, 'schema_version': 2}, 3)

    def test_old_wire_still_requires_exact_echo_and_cannot_choose_new_schema(self):
        old = {**self.content(), 'schema_version': 1, 'user_request': '原始请求', 'source_digest': 'a'*64}
        self.assertEqual(self.validate(old, 1), old)
        with self.assertRaisesRegex(StrategyError, 'strategy_identity_changed'):
            self.validate({**old, 'source_digest': 'a'*63}, 1)
        with self.assertRaises(StrategyError):
            self.validate(self.content(), 1)
        with self.assertRaises(StrategyError):
            self.validate(old, 2)


if __name__ == '__main__':
    unittest.main()
