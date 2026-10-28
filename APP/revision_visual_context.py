"""Carry verified same-frame historical layer observations into a bounded revision."""
from bridge_contract import BridgeError

from saved_region_analysis import verify_saved_regions


def augment_packet(binding, packet, *, root_binding, row, directory):
    """The caller first verifies the published root through its ordinary candidate guard."""
    if packet is None or not row.get('saved_region_manifest_sha256'):
        return packet
    expected_root = binding['native_lineage']['root_job_id']
    if (row.get('state') != 'available' or row.get('request_id') != expected_root
            or root_binding.get('request_id') != expected_root
            or row.get('owner') != binding['owner']
            or row.get('master_id') != binding['source_id']
            or row.get('master_sha256') != binding['source_sha256']
            or any(root_binding.get(key) != binding.get(key) for key in
                   ('owner', 'source_id', 'source_sha256', 'user_request', 'output_kind'))):
        raise BridgeError('revision_visual_scope_mismatch')
    projection = verify_saved_regions(directory, row)
    # Older area boxes are not upgraded to candidate-layer observations.
    if projection is None or projection.get('schema_version') not in {2, 3}:
        return packet
    expected_frames = [(frame['id'], frame['sha256']) for frame in packet['frames']]
    observed_frames = [(frame['frame_id'], frame['frame_sha256']) for frame in projection['frames']]
    if observed_frames != expected_frames or packet.get('source_sha256') != binding['source_sha256']:
        raise BridgeError('revision_visual_frames_mismatch')
    return {**packet, 'generation_region_projection': projection,
            'saved_region_reuse_reference': {'request_id': expected_root,
                'manifest_sha256': row['saved_region_manifest_sha256'],
                'role': 'historical_unverified_machine_observations', 'new_visual_calls': 0}}
