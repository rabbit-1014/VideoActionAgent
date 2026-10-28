"""Configure the local UI's existing pipeline without implicit source fallback."""

import os

from sqlalchemy import text

from .audio_session import AudioSession
from .browser_session import LocalOpenCliSessionProbe
from .deepseek_provider import DeepSeekProvider
from .product_pipeline import ProductPipeline
from .product_worker import ProductTaskWorker
from .source_access import LocalOpenCli
from .zhipu_provider import ZhipuProvider


class UIProcessingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def build_ui_worker(runtime, owner, source_modes, *, provider_name="zhipu",
                    provider_factory=None, node=None, opencli_entry=None,
                    navigation_workaround=False):
    modes = tuple(source_modes)
    if not modes or any(mode not in {"subtitle", "audio_transcription"} for mode in modes):
        raise ValueError("Explicit supported source modes required")
    audio = "audio_transcription" in modes
    browser = "subtitle" in modes
    if audio:
        with runtime.engine.connect() as connection:
            heads = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
        if len(heads) != 1 or not heads[0].isdigit() or int(heads[0]) < 11:
            raise UIProcessingError("audio_schema_upgrade_required")
    if browser and (node is None or opencli_entry is None or not opencli_entry.is_file()):
        raise UIProcessingError("browser_configuration_required")
    if provider_factory is not None:
        provider = provider_factory()
    elif provider_name == "deepseek":
        provider = DeepSeekProvider()
    elif provider_name == "zhipu":
        provider = ZhipuProvider(os.environ.get("VIDEO_ZHIPU_MODEL_ID", "glm-5.3"))
    else:
        raise ValueError("Unsupported generation provider")
    pipeline = ProductPipeline(
        runtime, owner,
        LocalOpenCli(node, opencli_entry, navigation_workaround=navigation_workaround)
        if browser else None,
        LocalOpenCliSessionProbe(node, opencli_entry) if browser else None,
        provider, audio_session_factory=AudioSession if audio else None,
    )
    return ProductTaskWorker(runtime, pipeline, owner, source_modes=modes)
