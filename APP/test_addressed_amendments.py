"""Frozen line addresses replace content without model-echoed anchors or hashes."""
import json
import unittest

from bridge_contract import BridgeError


class AddressedAmendmentsTests(unittest.TestCase):
    def test_repeated_lines_are_addressed_without_touching_other_text(self):
        from addressed_amendments import apply_reply, build_prompt
        parent = '# 标题\r\n\r\n- 老板 AI/CEO 助手\r\n- 同一句\r\n- 同一句\r\n'
        binding = {'request_id': 'frozen-request', 'source_sha256': 'a'*64,
                   'user_request': '整理全文', 'revision_request': {'instruction': '只修第二处重复行'}}
        prompt = build_prompt(binding, parent, {'transcription': {'segments': []}})
        context = json.loads(prompt.rsplit('\n', 1)[-1])
        self.assertEqual(context['parent_lines'][4], {'line_id': 'L0005', 'text': '- 同一句'})
        self.assertEqual(context['revision_instruction'], '只修第二处重复行')
        raw = json.dumps({'edits': [{'line_id': 'L0005', 'after': '- 第二处已修\n- 新说明'}]}, ensure_ascii=False)
        revised, proof = apply_reply(raw, parent, request_id='frozen-request', source_sha256='a'*64)
        self.assertEqual(revised, '# 标题\r\n\r\n- 老板 AI/CEO 助手\r\n- 同一句\r\n- 第二处已修\n- 新说明\r\n')
        self.assertEqual(proof['operations'][0]['before'], '- 同一句')
        self.assertEqual(proof['operations'][0]['start'], parent.rfind('- 同一句'))
        self.assertEqual(proof['request_id'], 'frozen-request')

    def test_unknown_duplicate_blank_noop_or_unbounded_edits_are_rejected(self):
        from addressed_amendments import apply_reply
        parent = '# 标题\n\n正文\n'
        cases = [
            {'edits': [{'line_id': 'L9999', 'after': '换'}]},
            {'edits': [{'line_id': 'L0002', 'after': '换'}]},
            {'edits': [{'line_id': 'L0003', 'after': '正文'}]},
            {'edits': [{'line_id': 'L0003', 'after': 'a'}, {'line_id': 'L0003', 'after': 'b'}]},
            {'edits': [{'line_id': 3, 'after': '换'}]},
            {'edits': [{'line_id': 'L0003', 'after': 'a'*8001}]},
            {'edits': [{'line_id': 'L0003', 'after': '换', 'before': '正文'}]},
            {'edits': [], 'request_id': 'forged'},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:100]), self.assertRaises(BridgeError):
                apply_reply(json.dumps(payload), parent, request_id='request', source_sha256='a'*64)
        for raw in ('{"edits":[],"edits":[]}', '{"edits":NaN}', '```json\n{"edits":[]}\n```'):
            with self.subTest(raw=raw), self.assertRaises(BridgeError):
                apply_reply(raw, parent, request_id='request', source_sha256='a'*64)
        with self.assertRaises(BridgeError):
            apply_reply('{"edits":[{"line_id":"L0001","after":""}]}', 'whole',
                        request_id='request', source_sha256='a'*64)


if __name__ == '__main__':
    unittest.main()
