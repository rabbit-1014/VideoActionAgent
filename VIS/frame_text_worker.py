"""Reuse the bundled RapidOCR ONNX models locally; never download model assets."""
import hashlib
import json
import sys
from pathlib import Path

VENDOR=Path('D:/CodexWork/VideoActionAgent-visual-grounding-20261017/ocr-vendor')
sys.path.insert(0,str(VENDOR))
from rapidocr_onnxruntime import RapidOCR


def main(path):
    frames=json.loads(Path(path).read_bytes())
    if not isinstance(frames,list) or not 1<=len(frames)<=3:
        raise ValueError('invalid_ocr_frame_count')
    models=VENDOR/'rapidocr_onnxruntime/models'
    weights={name:hashlib.sha256((models/name).read_bytes()).hexdigest() for name in (
        'ch_PP-OCRv4_det_infer.onnx','ch_PP-OCRv4_rec_infer.onnx','ch_ppocr_mobile_v2.0_cls_infer.onnx')}
    engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=1)
    result=[]
    for frame in frames:
        if hashlib.sha256(Path(frame['path']).read_bytes()).hexdigest()!=frame['sha256']:
            raise ValueError('visual_frame_changed')
        raw,_=engine(frame['path'])
        lines=[{'box':box,'text':text,'score':float(score)} for box,text,score in (raw or [])]
        result.append({'frame_id':frame['id'],'frame_sha256':frame['sha256'],
            'state':'available' if lines else 'empty','lines':lines,'engine':'rapidocr-onnxruntime/1.4.4',
            'model_sha256':weights,'network_enabled':False})
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main(sys.argv[1])
