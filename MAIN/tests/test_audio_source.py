import asyncio

import pytest

from video_action_agent.audio_source import AudioSourceError, resolve_audio


class Client:
    def __init__(self, wrong_page=False, fail=False, wrong_stream=False):
        self.calls = []
        self.wrong_page = wrong_page
        self.fail = fail
        self.wrong_stream = wrong_stream

    async def get_info(self):
        return {"bvid": "BV1AJen6SEVQ", "title": "collection", "pages": [
            {"page": 1 if self.wrong_page else 42, "cid": 420123,
             "part": "target", "duration": 10}]}

    async def get_download_url(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("signed url must not be exposed")
        cid = 999 if self.wrong_stream else 420123
        return {"timelength": 10000, "dash": {"duration": 10, "audio": [
            {"id": 30216, "baseUrl": f"https://audio.bilivideo.com/{cid}/audio.m4s",
             "bandwidth": 64000}]}}


def test_wrong_page_refused_before_stream_request():
    client = Client(wrong_page=True)
    with pytest.raises(AudioSourceError, match="identity"):
        asyncio.run(resolve_audio("BV1AJen6SEVQ", 42, client))
    assert client.calls == []


def test_exact_cid_and_page_and_no_retry_on_error():
    client = Client(fail=True)
    with pytest.raises(AudioSourceError, match="platform_request_failed") as caught:
        asyncio.run(resolve_audio("BV1AJen6SEVQ", 42, client))
    assert client.calls == [{"page_index": 41, "cid": 420123}]
    assert "signed url" not in str(caught.value)


def test_stream_cid_mismatch_refused():
    with pytest.raises(AudioSourceError, match="identity"):
        asyncio.run(resolve_audio("BV1AJen6SEVQ", 42, Client(wrong_stream=True)))


def test_exact_reference_has_no_signed_url_in_public_proof():
    resolved = asyncio.run(resolve_audio("BV1AJen6SEVQ", 42, Client()))
    assert resolved.proof()["cid"] == 420123
    assert resolved.proof()["page"] == 42
    assert "baseUrl" not in str(resolved.proof())
    assert "https://audio" not in repr(resolved)


def test_malformed_or_nonfinite_platform_data_safely_refused():
    class Malformed(Client):
        async def get_info(self):
            return {"bvid": "BV1AJen6SEVQ", "pages": None}

    class Nonfinite(Client):
        async def get_download_url(self, **kwargs):
            result = await super().get_download_url(**kwargs)
            result["timelength"] = float("nan")
            return result

    for client in (Malformed(), Nonfinite()):
        with pytest.raises(AudioSourceError, match="identity"):
            asyncio.run(resolve_audio("BV1AJen6SEVQ", 42, client))
