"""An owned revision retains pinned historical layers, without a new analysis."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from bridge_contract import BridgeError, digest

import test_saved_region_analysis as region_tests


class RevisionVisualContextTests(unittest.TestCase):
    def fixture(self, directory):
        fixture = region_tests.SavedRegionAnalysisTests()
        root, material, packet, answer = fixture.fixture(directory)
        root.update(goal_policy={'version': 14}, user_request='只区分画面层级', output_kind='source_answer')
        packet['scope_sha256'] = digest(root)
        fixture.repin_snapshot(directory, packet)
        answer['text'] = json.dumps({'frames': [{'frame_id': frame['id'],
            'editor_interface_visible': True, 'assignments': [
                {'text_index': index, 'role': role} for index, role in enumerate(
                    ('editor_interface', 'preview_content', 'overlay', 'unknown'), 1)]}
            for frame in packet['frames']]})
        from saved_region_analysis import analyse_saved_regions
        calls = []
        _, pinned_hash, _ = analyse_saved_regions(directory, root, material, packet,
            checkpoint=lambda: None, transport=lambda *_args: calls.append('initial_analysis') or answer)
        row = {'request_id': root['request_id'], 'owner': root['owner'], 'state': 'available',
               'master_id': root['source_id'], 'master_sha256': root['source_sha256'],
               'saved_region_manifest_sha256': pinned_hash}
        binding = {**root, 'request_id': 'revision', 'revision_strategy': 'line_text_edits_v1',
                   'revision_request': {'instruction': '解释为何预览文字不是软件身份'},
                   'native_lineage': {'root_job_id': root['request_id'], 'parent_job_id': 'parent', 'version': 3}}
        return root, row, binding, material, packet, calls

    def test_pinned_layers_reach_model_as_historical_observations_without_legacy_free_text(self):
        from addressed_amendments import build_prompt
        from revision_visual_context import augment_packet
        with tempfile.TemporaryDirectory() as temp:
            root, row, binding, material, packet, calls = self.fixture(Path(temp))
            original = copy.deepcopy(packet)
            augmented = augment_packet(binding, packet, root_binding=root, row=row, directory=Path(temp))
            prompt = build_prompt(binding, '# 原稿\n\n只区分画面。', material, packet=augmented)
            context = json.loads(prompt.rsplit('\n', 1)[-1])
            visual = context['saved_visual_packet']
            self.assertEqual(visual['new_visual_calls'], 0)
            self.assertIn('素材预览', prompt)
            self.assertIn('历史', prompt)
            self.assertNotIn('preview_content', prompt)
            self.assertNotIn('old free text must never survive', prompt)
            self.assertNotIn(str(temp), prompt)
            self.assertEqual(calls, ['initial_analysis'])
            self.assertEqual(packet, original)
            self.assertEqual(augmented['saved_region_reuse_reference']['request_id'], root['request_id'])

    def test_other_source_owner_frames_or_changed_original_are_rejected(self):
        from revision_visual_context import augment_packet
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            root, row, binding, _material, packet, calls = self.fixture(directory)
            for defect in ('owner', 'source', 'frames', 'not_available'):
                changed_row, changed_packet = copy.deepcopy(row), copy.deepcopy(packet)
                if defect == 'owner':
                    changed_row['owner'] = 'another'
                elif defect == 'source':
                    changed_row['master_sha256'] = 'b'*64
                elif defect == 'frames':
                    changed_packet['frames'][0]['sha256'] = 'b'*64
                else:
                    changed_row['state'] = 'failed'
                with self.subTest(defect=defect), self.assertRaises(BridgeError):
                    augment_packet(binding, changed_packet, root_binding=root, row=changed_row, directory=directory)
            raw = directory/'saved-region-raw-reply.json'
            raw.write_text('{}', encoding='utf-8')
            with self.assertRaises(BridgeError):
                augment_packet(binding, packet, root_binding=root, row=row, directory=directory)
            self.assertEqual(calls, ['initial_analysis'])


if __name__ == '__main__':
    unittest.main()
