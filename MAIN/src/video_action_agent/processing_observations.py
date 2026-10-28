"""Bounded process-local hints; task and completion facts stay in the database."""

from collections import OrderedDict
from datetime import UTC, datetime
from threading import Lock

MESSAGES = {
    "waiting_for_browser": "当前浏览器连接或登录尚未核实，本次未取得可读取会话。请保持Chrome、B站登录和Browser Bridge可用。",
    "retryable_failure": "这次成果生成未成功，已保存的来源仍保留，处理器稍后会重试生成。",
    "waiting_for_local_asr": "本机音轨识别处理入口尚未就绪，本次未执行新的音轨读取。",
    "requires_source_reconciliation": "来源操作的结果尚不能确认，需要核对已有操作；不会自动重复读取。",
    "dispatch_refused": "本次来源读取没有取得派发资格，未据此宣称读取成功。",
    "outcome_unknown": "本次来源操作的结果未知，不能据此判断视频没有内容。",
    "failed": "本次来源处理明确失败，不能据此判断视频没有字幕或语音。",
}


class ProcessingObservations:
    def __init__(self, limit=256):
        if limit < 1:
            raise ValueError("Positive observation limit required")
        self._limit = limit
        self._items = OrderedDict()
        self._lock = Lock()

    def record(self, event):
        key = (event["owner"], str(event["task_id"]))
        result = event["result"]
        state = result.get("state")
        stage = result.get("stage")
        with self._lock:
            if state not in MESSAGES or stage not in {"source", "artifact"}:
                self._items.pop(key, None)
                return
            self._items[key] = {
                "stage": stage, "state": state, "message": MESSAGES[state],
                "observed_at": datetime.now(UTC).isoformat(),
                "retry_at": event.get("retry_at"), "revision": event["revision"],
                "contract_version": event["contract_version"],
            }
            self._items.move_to_end(key)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)

    def read(self, owner, task):
        if task.get("completed") or task["contract_state"] == "canceled":
            return None
        with self._lock:
            hint = self._items.get((owner, str(task["task_id"])))
            if hint is None or (hint["revision"], hint["contract_version"]) != (
                task["revision"], task["current_version"],
            ):
                return None
            return dict(hint)
