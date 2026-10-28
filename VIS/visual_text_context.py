"""Persist a source-bound OCR text projection without rewriting visual history."""
import hashlib
import json

from bridge_contract import BridgeError, atomic

from visual_grounding import build_text_projection


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_text_context(directory, packet, packet_sha256, reader):
    frames=packet['frames']
    ocr=reader(frames)
    expected={frame['id']:frame['sha256'] for frame in frames}
    if (len(ocr)!=len(expected) or
            {frame['frame_id']:frame['frame_sha256'] for frame in ocr}!=expected):
        raise BridgeError('ocr_source_mismatch')
    projection=build_text_projection(packet['observation'],ocr)
    path=directory/'text-grounding.json'
    atomic(path,{'schema_version':1,'source':packet['source'],'source_sha256':packet['source_sha256'],
        'visual_manifest_sha256':packet_sha256,'ocr':ocr,'generation_text_projection':projection,
        'quality_approved':False})
    return projection,file_sha(path)


def verify_text_context(directory, row):
    expected=row.get('text_grounding_manifest_sha256')
    if expected is None:
        return
    path=directory/'text-grounding.json'
    if not path.is_file() or file_sha(path)!=expected:
        raise BridgeError('text_grounding_changed')
    value=json.loads(path.read_bytes())
    if (value['source_sha256']!=row['master_sha256'] or
            value['visual_manifest_sha256']!=row['vision_manifest_sha256']):
        raise BridgeError('text_grounding_source_changed')


def text_context_hint(projection):
    text=('\n当前选帧文字对照：只提供视觉识别与独立OCR同帧逐字匹配的文字候选；'
          '没有把原视觉模型的自由描述作为事实传入。两工具一致仍可能误字，不能当作已核验真值。'
          '不从标题或文字推定外层软件版别、素材文件类型或未提供的参数；'
          '这些文字也不足以确认颜色、动作、界面区域关系、代码连线或完整实现，依赖它们的问题明确不足。'
          '材料未提及某项内容不等于两者无关、不存在或不会发生；没有关系证据时只说明无法确认，不能把缺席改写为否定结论。'
          '仅回答用户所问，不附加无关素材元数据；转写、画面文字和推断分别归因。'
          '正文不要显示内部帧ID、哈希或工具审计标签，用普通语言说明必要的不确定性。\n')
    if projection is None:
        return text+'本次没有可用的独立文字对照，不回退采用原视觉模型断言。'
    return text+json.dumps(projection,ensure_ascii=False)
