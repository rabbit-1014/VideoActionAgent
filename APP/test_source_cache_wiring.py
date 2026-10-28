"""The normal entry supplies only current-owner, validated matching sources."""
import copy
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import purpose_app
from cached_capture import CACHES
from video_action_agent.source_access import VideoRef
from video_action_agent.source_candidate_bundle import read_bundle, save_bundle
from video_action_agent.source_capture import SourceCapture


class SourceCacheWiringTests(unittest.TestCase):
    def test_persisted_owner_sources_are_reusable_but_other_owner_and_other_part_are_not(self):
        saved_source_bundles = getattr(purpose_app, 'saved_source_bundles', None)
        self.assertTrue(callable(saved_source_bundles), 'normal entry has no owner-scoped cache supplier')
        bundle = read_bundle(CACHES[0])
        bundle['acquisition'] = 'live_anonymous_audio'
        proof = bundle['source_envelope']['source_evidence']
        source = f"https://www.bilibili.com/video/{proof['bvid']}?p={proof['page']}"
        ref = VideoRef.parse(source)
        with tempfile.TemporaryDirectory() as directory:
            def capture(_source, output, **_kwargs):
                return save_bundle(copy.deepcopy(bundle), output)
            first = SourceCapture('alice', Path(directory), capture_fn=capture)
            identity = str(uuid4())
            first.start(identity, source, allow_local_audio_read=True)
            first.wait(identity, 10)
            self.assertEqual(first.get(identity)['state'], 'available', first.get(identity))
            cold = SourceCapture('alice', Path(directory))
            self.assertEqual(list(saved_source_bundles(cold, ref)), [bundle])
            other_part = VideoRef.parse(f"https://www.bilibili.com/video/{ref.bvid}?p={ref.page + 1}")
            self.assertEqual(list(saved_source_bundles(cold, other_part)), [])
            self.assertEqual(list(saved_source_bundles(SourceCapture('bob', Path(directory)), ref)), [])


if __name__ == '__main__':
    unittest.main()
