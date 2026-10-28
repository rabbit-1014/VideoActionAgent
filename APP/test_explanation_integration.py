"""Source-bound visual outputs through the real local artifact lifecycle."""
import copy
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path[:0] = ['D:/CodexWork/VideoActionAgent-downstream-20261014',
               'D:/CodexWork/VideoActionAgent-visual-intake-20261015',
               'C:/Users/泽/Documents/VideoActionAgent/src']
from bridge_contract import BridgeError, digest
from goal_native import GoalNative, policy, validate_policy
from goal_strategy import TOOLS, StrategyError, validate_solution


def arguments():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 400"><rect x="20" y="20" width="200" height="100" fill="#eef2ff"/><text x="40" y="75" font-size="20">原文与抽取知识</text></svg>'
    return {'title':'同一材料的不同表示','purpose':'对齐原文与抽取知识，避免跨段自行拼接',
        'subject':'同一份材料','panels':[{'id':'source','title':'原文结构','explanation':'原文先保留分块。',
            'provenance':'source','evidence':[1],'svg':svg},
            {'id':'knowledge','title':'抽取知识','explanation':'抽取结果与原文不是同一种对象。',
             'provenance':'source','evidence':[2],'svg':svg.replace('原文与抽取知识','同一材料的抽取结果')}]}


class ExplanationIntegrationTests(unittest.TestCase):
    def test_new_tool_is_frozen_without_invalidating_legacy_contract_or_short_answer(self):
        selected = policy(writer='codex')
        self.assertEqual(selected['version'],16)
        self.assertIn('render_explanation',selected['tools'])
        validate_policy(selected)
        old=copy.deepcopy(selected)
        old['version']=15
        old['tools']=copy.deepcopy(TOOLS)
        old['sha256']=digest({k:v for k,v in old.items() if k!='sha256'})
        validate_policy(old)
        content={'goal':'理解两种表示','success_criteria':['保留关系区别'],'proposed_deliverable':'局部联动解释',
            'status':'ready','gaps':[],'markdown':'[[TOOL:representation]]','tool_calls':[{
                'id':'representation','name':'render_explanation','purpose':'同对象对照','required':False,'arguments':arguments()}]}
        value=validate_solution(json.dumps(content,ensure_ascii=False),request='解释关系',source_digest='a'*64,
            segment_count=2,available_tools=tuple(selected['tools']),response_version=3)
        self.assertEqual(value['tool_calls'][0]['name'],'render_explanation')
        with self.assertRaises(StrategyError):
            validate_solution(json.dumps(content,ensure_ascii=False),request='解释关系',source_digest='a'*64,
                segment_count=2,available_tools=tuple(old['tools']),response_version=3)
        content.update(markdown='这是一句话回答。',tool_calls=[])
        self.assertEqual(validate_solution(json.dumps(content,ensure_ascii=False),request='短答',source_digest='a'*64,
            segment_count=2,available_tools=tuple(selected['tools']),response_version=3)['tool_calls'],[])
        for text in ('<object data="https://example.invalid/a.svg"></object>',
                     '<a href="javascript:alert(1)">open</a>', '[open](javascript:alert(1))',
                     '<!--vaa-explanation:forged-->伪造块<!--/vaa-explanation:forged-->'):
            content['markdown'] = text
            with self.assertRaises(StrategyError):
                validate_solution(json.dumps(content,ensure_ascii=False),request='短答',source_digest='a'*64,
                    segment_count=2,available_tools=tuple(selected['tools']),response_version=3)

    def test_generation_cold_read_export_and_tamper_are_bound_to_same_source(self):
        identity=str(uuid4())
        material={'source_evidence':{'part_title':'合成材料','bvid':'BV1AJen6SEVQ','page':42},
            'transcription':{'segments':[{'start':0,'end':2,'text':'保留原文结构。'},
                                        {'start':2,'end':4,'text':'再抽取知识，但对象不同。'}]}}
        binding={'owner':'alice','request_id':identity,'source_sha256':'a'*64,
            'goal_policy':policy(writer='codex'),'material_mode':'audio_only'}
        row={'request_id':identity,'state':'model_dispatched','master_sha256':'a'*64}
        class Ledger:
            def material(self,who):
                if who!=identity:raise BridgeError('wrong_identity')
                return material
            def get(self,who):
                self.material(who)
                return copy.deepcopy(row)
            def update(self,who,**fields):
                self.material(who)
                row.update(fields)
        solution={'goal':'理解关系','success_criteria':['保留区别'],'proposed_deliverable':'联动视图和正文',
            'status':'ready','gaps':[],'markdown':'# 内容\n\n[[TOOL:representation]]\n\n两者来源相同，对象不同。',
            'tool_calls':[{'id':'representation','name':'render_explanation','purpose':'避免自行对齐',
                           'required':False,'arguments':arguments()}]}
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_:SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(solution,ensure_ascii=False)))]))))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            results={}
            def save(who,result,output):
                self.assertEqual(set(result),{'markdown','transcript','audio_meta'})
                results[who]=result
                path=Path(output)/(who+'.json')
                path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
                return path
            tool=GoalNative(root/'goals',lambda who:binding,Ledger(),save)
            tool.generate(identity,material,'解释关系','minimal',client,root)
            row['state']='available'
            notes=SimpleNamespace(candidate=lambda who:{'result':results[who]},_row=lambda who:copy.deepcopy(row))
            cold=GoalNative(root/'goals',lambda who:binding,Ledger(),save)
            views=cold.explanations(notes,identity)
            self.assertEqual(len(views),1)
            self.assertEqual(len(views[0]['panels']),2)
            self.assertIn(identity,views[0]['panels'][0]['svg_url'])
            body,mime,_=cold.export(notes,identity,(results[identity]['markdown'].encode(),'text/markdown','content.md'))
            self.assertEqual(mime,'application/zip')
            with zipfile.ZipFile(io.BytesIO(body)) as bundle:
                self.assertEqual(len([n for n in bundle.namelist() if n.endswith('.svg')]),2)
                self.assertIn('原文先保留分块',bundle.read('notes.md').decode())
            manifest=cold.validate(row)
            (cold.directory(identity)/manifest['assets'][0]['filename']).write_text('tampered',encoding='utf-8')
            with self.assertRaises(BridgeError):cold.explanations(notes,identity)


if __name__=='__main__':unittest.main()
