"""Frozen explanation updates stay inside runtime-owned blocks and identities."""
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path[:0] = ['D:/CodexWork/VideoActionAgent-downstream-20261014',
               'D:/CodexWork/VideoActionAgent-visual-intake-20261015',
               'C:/Users/泽/Documents/VideoActionAgent/src']
from bridge_contract import BridgeError
from native_amendment import NativeAmendment


def arguments():
    return {'title': '两个维度', 'purpose': '避免把两个维度混为一谈', 'subject': '同一观察框架',
            'panels': [{'id': 'dimensions', 'title': '同时考虑', 'explanation': '两个维度分别判断。',
                        'provenance': 'source', 'evidence': [1],
                        'svg': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 400"><text x="20" y="30">两个维度</text></svg>'}]}


class ExplanationAmendmentsTests(unittest.TestCase):
    def setUp(self):
        self.bound = [{'call_id': 'dimensions', 'arguments': arguments()}]
        self.parent = ('# 原标题\r\n保留外部说明。\r\n<!--vaa-explanation:dimensions-->\r\n'
                       '![原图](assets/old.svg)\r\n原图说明。\r\n<!--/vaa-explanation:dimensions-->\r\n'
                       '尾部原文不动。\r\n')
        self.material = {'transcription': {'segments': [{'text': '两个维度分别判断。'}]}}
        self.binding = {'request_id': 'revision', 'source_sha256': 'a' * 64,
                        'user_request': '整理原视频', 'goal_policy': {'version': 16,
                            'generation_provider': {'id': 'codex_local', 'model': 'gpt-6.1-sol',
                                'reasoning_effort': 'high', 'transport_version': 1}},
                        'revision_strategy': 'line_text_edits_v1',
                        'revision_request': {'instruction': '只修改图中说明，保留正文'}}

    def client(self, reply, prompts):
        def create(**kwargs):
            prompts.append(kwargs['messages'][0]['content'])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def test_native_graph_update_preserves_external_text_source_and_three_key_save(self):
        updated = arguments()
        updated['panels'][0]['explanation'] = '分别判断，不能用一个维度代替另一个。'
        updated['panels'][0]['svg'] = updated['panels'][0]['svg'].replace('两个维度</text>', '分别判断</text>')
        reply = json.dumps({'edits': [], 'explanation_updates': [{'call_id': 'dimensions', 'arguments': updated}]}, ensure_ascii=False)
        previous = {'markdown': self.parent, 'transcript': copy.deepcopy(self.material['transcription']), 'audio_meta': {'source': 'fixed'}}
        frozen = copy.deepcopy((previous, self.material, self.bound))
        saved, records, prompts = [], [], []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'revision').mkdir()
            amendment = NativeAmendment(lambda *a, **k: self.fail('unexpected generation'),
                lambda identity, result, output: saved.append(copy.deepcopy(result)),
                lambda identity: self.binding,
                lambda identity: (previous, {'bound_explanations': self.bound}),
                lambda *args: records.append(args), root)
            try:
                amendment('revision', self.material, '修改图', 'minimal', self.client(reply, prompts), root)
            except BridgeError as exc:
                self.fail('explanation amendment route is unavailable: ' + str(exc))
            expected = ('# 原标题\r\n保留外部说明。\r\n<!--vaa-explanation:dimensions-->\n'
                        '[[TOOL:dimensions]]\n<!--/vaa-explanation:dimensions-->\r\n尾部原文不动。\r\n')
            self.assertEqual(saved[0], {**previous, 'markdown': expected})
            self.assertEqual(set(saved[0]), {'markdown', 'transcript', 'audio_meta'})
            proof_path = root / 'revision' / 'amendment.json'
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            self.assertEqual(proof['mode'], 'frozen_explanation_edits')
            self.assertEqual(proof['explanation_updates'][0]['arguments'], updated)
            self.assertEqual(proof['request_id'], 'revision')
            self.assertEqual(proof['source_sha256'], 'a' * 64)
            self.assertEqual(proof['parent_markdown_sha256'], hashlib.sha256(self.parent.encode()).hexdigest())
            self.assertEqual(proof['result_markdown_sha256'], hashlib.sha256(expected.encode()).hexdigest())
            self.assertFalse(proof['semantic_quality_approved'])
            self.assertEqual(records, [('revision', hashlib.sha256(proof_path.read_bytes()).hexdigest())])
            context = json.loads(prompts[0].rsplit('\n', 1)[-1])
            self.assertEqual(context['bound_explanations'], self.bound)
            self.assertEqual(context['requested_material']['transcription']['segments'][0]['evidence_index'], 1)
            self.assertFalse(context['parent_lines'][2]['editable'])
        self.assertEqual((previous, self.material, self.bound), frozen)
        from explanation_amendments import apply_reply
        plain = '# 原标题\r\n保留外部说明。\r\n尾部原文不动。\r\n'
        revised, proof = apply_reply(json.dumps({'edits': [], 'new_explanations': [
            {'after_line_id': 'L0002', 'arguments': arguments()}]}), plain,
            request_id='revision', source_sha256='a' * 64, bound_explanations=[],
            segment_count=1, reserved_call_ids=['added_explanation_1'], max_new_explanations=1)
        block = ('<!--vaa-explanation:added_explanation_2-->\n[[TOOL:added_explanation_2]]\n'
                 '<!--/vaa-explanation:added_explanation_2-->')
        self.assertEqual(revised, plain.replace('保留外部说明。', '保留外部说明。\n' + block))
        self.assertEqual(proof['explanation_additions'], [
            {'call_id': 'added_explanation_2', 'after_line_id': 'L0002', 'arguments': arguments()}])
        self.assertEqual(proof['explanation_removals'], [])
        self.assertEqual(proof['parent_markdown_sha256'], hashlib.sha256(plain.encode()).hexdigest())
        self.assertEqual(proof['result_markdown_sha256'], hashlib.sha256(revised.encode()).hexdigest())
        removed, proof = apply_reply(json.dumps({'edits': [{'line_id': 'L0007', 'after': '新的尾部。'}],
            'explanation_removals': ['dimensions'], 'new_explanations': [
                {'after_line_id': 'L0002', 'arguments': arguments()}]}), self.parent,
            request_id='revision', source_sha256='a' * 64, bound_explanations=self.bound,
            segment_count=1, reserved_call_ids=['dimensions'], max_new_explanations=0)
        expected_block = block.replace('added_explanation_2', 'added_explanation_1')
        self.assertEqual(removed, '# 原标题\r\n保留外部说明。\n' + expected_block + '\r\n\r\n新的尾部。\r\n')
        self.assertEqual(proof['explanation_removals'], ['dimensions'])
        self.assertEqual(proof['explanation_updates'], [])
        removed, proof = apply_reply('{"edits":[],"explanation_removals":["dimensions"]}', self.parent,
            request_id='revision', source_sha256='a' * 64, bound_explanations=self.bound, segment_count=1)
        self.assertEqual(removed, '# 原标题\r\n保留外部说明。\r\n\r\n尾部原文不动。\r\n')
        self.assertEqual(proof['explanation_additions'], [])
        two, proof = apply_reply(json.dumps({'edits': [{'line_id': 'L0001', 'after': '# 新标题'}],
            'new_explanations': [{'after_line_id': 'L0003', 'arguments': arguments()},
                                 {'after_line_id': 'L0002', 'arguments': arguments()}]}), plain.rstrip('\r\n'),
            request_id='revision', source_sha256='a' * 64, bound_explanations=[], segment_count=1)
        second = expected_block.replace('added_explanation_1', 'added_explanation_2')
        self.assertEqual(two, '# 新标题\r\n保留外部说明。\n' + second + '\r\n尾部原文不动。\n' + expected_block)
        self.assertEqual([item['call_id'] for item in proof['explanation_additions']],
                         ['added_explanation_1', 'added_explanation_2'])

    def test_forged_addresses_owned_blocks_invalid_updates_and_svg_are_rejected(self):
        self.assertIsNotNone(importlib.util.find_spec('explanation_amendments'), 'new bounded module is missing')
        from explanation_amendments import apply_reply
        updated = arguments()
        updated['panels'][0]['explanation'] = '修正说明。'
        valid_update = {'call_id': 'dimensions', 'arguments': updated}
        cases = [
            {'edits': [], 'explanation_updates': []},
            {'edits': [{'line_id': 'L9999', 'after': '伪地址'}], 'explanation_updates': []},
            {'edits': [{'line_id': 'L0003', 'after': '改标记'}], 'explanation_updates': []},
            {'edits': [{'line_id': 'L0004', 'after': '改图'}], 'explanation_updates': []},
            {'edits': [], 'explanation_updates': [{'call_id': 'unknown', 'arguments': updated}]},
            {'edits': [], 'explanation_updates': [valid_update, valid_update]},
            {'edits': [], 'explanation_updates': self.bound},
        ]
        for injected in ('[[TOOL:fake]]', '<img src="https://bad.invalid/x">',
                         '![替换](https://bad.invalid/x)', '<!--vaa-explanation:fake-->',
                         '<!DOCTYPE html>', '<?instruction unsafe?>',
                         '[open](javascript:alert(1))', '[open](JaVaScRiPt:alert(1))',
                         '[open](java&#x73;cript&colon;alert(1))', '[open](java\t\nscript:alert(1))',
                         '[open](javascript\\:alert(1))', '[open](data:text/html,bad)',
                         '[open](vbscript:bad)', '[open](file:///private)',
                         '[open][target]\n[target]: data:text/html,bad',
                         '&lt;javascript:alert(1)&gt;'):
            cases.append({'edits': [{'line_id': 'L0002', 'after': injected}], 'explanation_updates': []})
            invalid = arguments()
            invalid['title'] = injected
            cases.append({'edits': [], 'new_explanations': [{'after_line_id': 'L0002', 'arguments': invalid}]})
        for field, value in (('svg', '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 400"><script>alert(1)</script></svg>'),
                             ('evidence', [2]), ('explanation', '<iframe src="https://bad.invalid"></iframe>')):
            invalid = copy.deepcopy(updated)
            invalid['panels'][0][field] = value
            cases.append({'edits': [], 'explanation_updates': [{'call_id': 'dimensions', 'arguments': invalid}]})
            cases.append({'edits': [], 'new_explanations': [{'after_line_id': 'L0002', 'arguments': invalid}]})
        new = {'after_line_id': 'L0002', 'arguments': arguments()}
        cases.extend([
            {'edits': [], 'new_explanations': [new, new]},
            {'edits': [], 'new_explanations': [new, dict(new, after_line_id='L0001'), dict(new, after_line_id='L0007')]},
            {'edits': [], 'new_explanations': [dict(new, call_id='forged')]},
            {'edits': [], 'new_explanations': [dict(new, after_line_id='L9999')]},
            {'edits': [], 'new_explanations': [dict(new, after_line_id='L0003')]},
            {'edits': [], 'new_explanations': [dict(new, after_line_id='L0004')]},
            {'edits': [{'line_id': 'L0002', 'after': '修改'}], 'new_explanations': [new]},
            {'edits': [], 'explanation_removals': ['unknown']},
            {'edits': [], 'explanation_removals': ['dimensions', 'dimensions']},
            {'edits': [], 'explanation_removals': ['dimensions'], 'explanation_updates': [valid_update]},
            {'new_explanations': [new]},
        ])
        for payload in cases:
            with self.subTest(payload=str(payload)[:100]), self.assertRaises(BridgeError):
                apply_reply(json.dumps(payload), self.parent, request_id='revision', source_sha256='a' * 64,
                            bound_explanations=self.bound, segment_count=1)
        for options in ({'max_new_explanations': 0}, {'max_new_explanations': -1},
                        {'max_new_explanations': True}, {'reserved_call_ids': ['duplicate', 'duplicate']},
                        {'reserved_call_ids': ['dimensions', 'another_tool']}):
            with self.subTest(options=options), self.assertRaises(BridgeError):
                apply_reply(json.dumps({'edits': [], 'new_explanations': [new]}), self.parent,
                            request_id='revision', source_sha256='a' * 64,
                            bound_explanations=self.bound, segment_count=1, **options)
        with self.assertRaises(BridgeError):
            apply_reply(json.dumps({'edits': [], 'explanation_removals': ['dimensions'],
                'new_explanations': [new, dict(new, after_line_id='L0001')]}), self.parent,
                request_id='revision', source_sha256='a' * 64, bound_explanations=self.bound,
                segment_count=1, max_new_explanations=0)
        for raw in ('{"edits":[],"edits":[]}', '{"edits":[],"new_explanations":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(BridgeError):
                apply_reply(raw, self.parent, request_id='revision', source_sha256='a' * 64,
                            bound_explanations=self.bound, segment_count=1)
        for parent, target in ((' \n', 'L0001'), ('x' * 4001, 'L0001')):
            with self.subTest(parent=parent[:20]), self.assertRaises(BridgeError):
                apply_reply(json.dumps({'edits': [], 'new_explanations': [dict(new, after_line_id=target)]}),
                            parent, request_id='revision', source_sha256='a' * 64,
                            bound_explanations=[], segment_count=1)
        with self.assertRaises(BridgeError):
            apply_reply('{"edits":[],"explanation_removals":["dimensions"]}', self.parent,
                        request_id='revision', source_sha256='a' * 64, bound_explanations=[], segment_count=1)
        for parent in (self.parent + self.parent, self.parent.replace('dimensions-->', 'unknown-->'),
                       self.parent.replace('<!--/vaa-explanation:dimensions-->', ''),
                       self.parent.replace('<!--vaa-explanation:dimensions-->', '<!--vaa-explanation:dimensions -->')):
            with self.subTest(parent=parent), self.assertRaises(BridgeError):
                apply_reply(json.dumps({'edits': [], 'explanation_updates': [valid_update]}), parent,
                            request_id='revision', source_sha256='a' * 64,
                            bound_explanations=self.bound, segment_count=1)
        revised, proof = apply_reply(json.dumps({'edits': [{'line_id': 'L0002', 'after': '只改块外。'}],
                                                'explanation_updates': []}), self.parent,
                                     request_id='revision', source_sha256='a' * 64,
                                     bound_explanations=self.bound, segment_count=1)
        self.assertEqual(revised, self.parent.replace('保留外部说明。', '只改块外。'))
        self.assertEqual(proof['explanation_updates'], [])
        safe_text = 'data: 是元数据字段；[官方说明](https://example.invalid/docs)。\n[文档]: https://example.invalid/docs'
        revised, _ = apply_reply(json.dumps({'edits': [{'line_id': 'L0002', 'after': safe_text}],
                                             'explanation_updates': []}), self.parent,
                                  request_id='revision', source_sha256='a' * 64,
                                  bound_explanations=self.bound, segment_count=1)
        self.assertEqual(revised, self.parent.replace('保留外部说明。', safe_text))

    def test_legacy_policy_or_absent_bound_field_keeps_existing_line_edits(self):
        for version, packet in ((15, {'bound_explanations': self.bound}), (16, None), (16, {'bound_explanations': []})):
            with self.subTest(version=version, packet=packet), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'revision').mkdir()
                binding = copy.deepcopy(self.binding)
                binding['goal_policy']['version'] = version
                saved, prompts = [], []
                previous = {'markdown': '# 标题\n正文\n', 'transcript': {}, 'audio_meta': {}}
                amendment = NativeAmendment(lambda *a, **k: self.fail('unexpected generation'),
                    lambda identity, result, output: saved.append(result), lambda identity: binding,
                    lambda identity: (previous, packet), lambda *args: None, root)
                amendment('revision', self.material, '修改正文', 'minimal',
                          self.client('{"edits":[{"line_id":"L0002","after":"修改后"}]}', prompts), root)
                self.assertEqual(saved[0]['markdown'], '# 标题\n修改后\n')
                proof = json.loads((root / 'revision' / 'amendment.json').read_text(encoding='utf-8'))
                self.assertEqual(proof['mode'], 'frozen_line_offsets' if version <= 15 else 'frozen_explanation_edits')
                if version >= 16:
                    self.assertEqual(proof['explanation_additions'], [])
                    self.assertEqual(proof['explanation_removals'], [])
                    self.assertIn('new_explanations', prompts[0])
                    self.assertIn('explanation_removals', prompts[0])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'revision').mkdir()
            previous = {'markdown': '# 标题\n正文\n', 'transcript': {}, 'audio_meta': {}}
            saved, prompts = [], []
            packet = {'bound_explanations': [], 'reserved_call_ids': [], 'max_new_explanations': 2}
            amendment = NativeAmendment(lambda *a, **k: self.fail('unexpected generation'),
                lambda identity, result, output: saved.append(result), lambda identity: self.binding,
                lambda identity: (previous, packet), lambda *args: None, root)
            amendment('revision', self.material, '局部图解', 'minimal', self.client(json.dumps({
                'edits': [], 'new_explanations': [{'after_line_id': 'L0002', 'arguments': arguments()}]}), prompts), root)
            self.assertIn('[[TOOL:added_explanation_1]]', saved[0]['markdown'])
            self.assertEqual(set(saved[0]), {'markdown', 'transcript', 'audio_meta'})
            self.assertEqual(previous['markdown'], '# 标题\n正文\n')


if __name__ == '__main__':
    unittest.main()
