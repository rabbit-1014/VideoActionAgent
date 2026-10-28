"""Bounded local RapidOCR reuse; no network and no implicit model download."""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from bridge_contract import BridgeError, atomic

ROOT=Path(__file__).resolve().parent
WORKER=ROOT/'frame_text_worker.py'
CACHE=ROOT/'runtime/text-cache'


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_frame_text(frames):
    if not 1<=len(frames)<=3:
        raise BridgeError('invalid_ocr_frame_count')
    worker_sha=file_sha(WORKER)
    CACHE.mkdir(parents=True,exist_ok=True)
    result=[]
    for frame in frames:
        if file_sha(frame['path'])!=frame['sha256']:
            raise BridgeError('visual_frame_changed')
        key=hashlib.sha256((worker_sha+frame['sha256']).encode()).hexdigest()
        cache=CACHE/(key+'.json')
        stored=json.loads(cache.read_bytes()) if cache.is_file() else None
        if stored is not None and (stored['worker_sha256']!=worker_sha or stored['frame_sha256']!=frame['sha256']):
            raise BridgeError('ocr_cache_mismatch')
        if stored is None:
            with tempfile.TemporaryDirectory(dir=CACHE) as temporary:
                input_path=Path(temporary)/'frames.json'
                input_path.write_text(json.dumps([frame]),encoding='utf-8')
                try:
                    process=subprocess.run([sys.executable,str(WORKER),str(input_path)],
                        capture_output=True,timeout=45,check=True)
                    if len(process.stdout)>1_000_000:
                        raise ValueError('ocr_reply_over_limit')
                    value=json.loads(process.stdout.decode('utf-8-sig'))
                    if (not isinstance(value,list) or len(value)!=1 or value[0]['frame_id']!=frame['id']
                            or value[0]['frame_sha256']!=frame['sha256']):
                        raise ValueError('ocr_reply_identity_mismatch')
                    stored={**value[0],'worker_sha256':worker_sha,'network_enabled':False}
                except (OSError,subprocess.SubprocessError,ValueError,KeyError):
                    stored={'frame_id':frame['id'],'frame_sha256':frame['sha256'],'worker_sha256':worker_sha,
                            'state':'unavailable','lines':[],'error_code':'local_ocr_unavailable'}
                if file_sha(frame['path'])!=frame['sha256'] or file_sha(WORKER)!=worker_sha:
                    raise BridgeError('ocr_input_changed')
                if stored['state']!='unavailable':
                    atomic(cache,stored)
        result.append({**stored,'frame_id':frame['id']})
    return result
