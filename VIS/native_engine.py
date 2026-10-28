"""Run unchanged pinned BiliNote plugin on an already validated full source."""
import asyncio
import importlib
import json
import os
import sys
import types
from pathlib import Path

from bridge_contract import BridgeError, atomic
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse

from purpose_prompt import bound_prompt_version, purpose_gpt_class

NATIVE_BACKEND = Path("D:/CodexWork/VideoActionAgent-downstream-20261014/BiliNote/backend")
SOURCE_RULES = (
    "仅依据已保存转写。未取得画面、现行软件界面或完整代码时说明缺失。"
    "界面名称、软件别名、代码/API/参数标识、正则表达式必须在转写中有逐字依据；"
    "材料只描述作用或名称含混时用自然语言描述并列为待核，不能补成确定的代码标识。"
    "疑似语音识别错误的修正写明是推测，讲者的效果评价写明是视频说法。"
    "材料中提到的操作分支及步骤不能因摘要合并而遗漏；尤其排序、后续择优选择、"
    "字段与表示转换是不同动作，材料确实包含时分别保留目的和结果，未出现时不补造。"
)


class NoAcquisition:
    def __getattr__(self, name):
        raise BridgeError("native_source_reacquisition_not_authorized")


class NativeEngine:
    def __init__(self, runtime):
        self.runtime=Path(runtime)
        self.output=self.runtime/"notes/native"
        self.output.mkdir(parents=True,exist_ok=True)
        os.environ.update(NOTE_OUTPUT_DIR=str(self.output), DATABASE_URL="sqlite:///"+
                          (self.runtime/"native.sqlite").as_posix(), TRANSCRIBER_TYPE="bcut",
                          OPENAI_RETRY_ATTEMPTS="1",OPENAI_RETRY_BACKOFF_SECONDS="0")
        os.chdir(self.runtime)
        sys.path.insert(0,str(NATIVE_BACKEND))
        package=types.ModuleType("app")
        package.__path__=[str(NATIVE_BACKEND/"app")]
        sys.modules["app"]=package
        self.native=importlib.import_module("app.routers.note")
        service=importlib.import_module("app.services.note")
        engine=importlib.import_module("app.db.engine")
        engine.Base.metadata.create_all(engine.engine)
        universal=importlib.import_module("app.gpt.universal_gpt").UniversalGPT
        purpose_gpt=purpose_gpt_class(universal,importlib.import_module("app.gpt.prompt").BASE_PROMPT)
        self.clients={}
        clients=self.clients

        class SavedGenerator(service.NoteGenerator):
            def generate(self,*args,**kwargs):
                self.native_identity=kwargs["task_id"]
                return super().generate(*args,**kwargs)

            def _init_transcriber(self):
                return NoAcquisition()

            def _get_downloader(self,platform):
                return NoAcquisition()

            def _get_gpt(self,model_name,provider_id):
                if model_name!="glm-5.3" or provider_id!="env-zhipu":
                    raise BridgeError("provider_not_admitted")
                client=clients[self.native_identity]
                factory=purpose_gpt if bound_prompt_version(client,self.native_identity)>=2 else universal
                return factory(client,model_name)

        self.native.NoteGenerator=SavedGenerator

    def save_amendment(self,identity,result,output):
        """Reuse native NoteResult serialization without generating a second full draft."""
        from app.models.notes_model import NoteResult
        if Path(output)!=self.output or set(result)!={"markdown","transcript","audio_meta"}:
            raise BridgeError("native_amendment_output_mismatch")
        self.native.save_note_to_file(identity,NoteResult(**result))
        (self.output/(identity+"_markdown.md")).write_text(result["markdown"],encoding="utf-8")
        return self.output/(identity+".json")

    def generate(self,identity,material,request,style,client,output,*,extras_override=None,format_override=None):
        if Path(output)!=self.output:
            raise BridgeError("native_output_mismatch")
        proof=material["source_evidence"]
        segments=material["transcription"]["segments"]
        source_rules=SOURCE_RULES
        if bound_prompt_version(client,identity)>=2:
            source_rules=source_rules.replace("材料中提到的操作分支及步骤不能因摘要合并而遗漏", 
                "属于本次所选目标和方法的操作分支及步骤不能因摘要合并而遗漏")
        url=f"https://www.bilibili.com/video/{proof['bvid']}?p={proof['page']}"
        atomic(self.output/(identity+"_audio.json"), {
            "file_path":"", "cover_url":"", "title":proof["part_title"],
            "duration":segments[-1]["end"],"platform":"bilibili","video_id":proof["bvid"],
            "raw_info":{"source_role":"validated_source_capture","webpage_url":url,
                        "page":proof["page"],"cid":proof["cid"]}})
        data=self.native.VideoRequest(video_url=url,platform="bilibili",quality="medium",
            model_name="glm-5.3",provider_id="env-zhipu",task_id=identity,
            format=["toc"] if format_override is None else format_override,style=style,
            screenshot=False,link=False,video_understanding=False,
            prefetched_transcript={"language":material["transcription"].get("language","auto"),
                                  "segments":segments,"full_text":"\n".join(s["text"] for s in segments)},
            extras=(extras_override or "本次用户要求："+request)+"\n"+(
                source_rules if extras_override is None or "当前选帧" not in extras_override else
                source_rules.replace("仅依据已保存转写。", "依据已保存转写与明确标为候选的当前画面观察。", 1)
                .replace("必须在转写中有逐字依据", "必须在转写或清楚的画面文字中有依据；含混视觉识别不能确认为代码", 1)))
        callbacks=BackgroundTasks()
        self.clients[identity]=client
        try:
            response=self.native.generate_note(data,callbacks)
            if not isinstance(response,JSONResponse) or json.loads(response.body).get("code")!=0:
                raise BridgeError("native_admission_failed")
            asyncio.run(callbacks())
        finally:
            self.clients.pop(identity,None)
        path=self.output/(identity+".json")
        if not path.is_file():
            raise BridgeError("native_failed_or_unconfirmed")
        return path
