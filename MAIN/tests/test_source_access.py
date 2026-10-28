import importlib
import json

import pytest


def api():
    assert importlib.util.find_spec("video_action_agent.source_access"), "source access missing"
    return importlib.import_module("video_action_agent.source_access")


@pytest.mark.parametrize(
    "source,page",
    [
        ("BV1gD421u75J", 1),
        ("https://www.bilibili.com/video/BV1gD421u75J/?p=2&share_source=copy", 2),
    ],
)
def test_reference(source, page):
    ref = api().VideoRef.parse(source)
    assert ref.bvid == "BV1gD421u75J"
    assert ref.page == page


@pytest.mark.parametrize(
    "source",
    [
        "https://www.bilibili.com.evil.test/video/BV1gD421u75J",
        "https://user:pass@www.bilibili.com/video/BV1gD421u75J",
        "https://127.0.0.1/video/BV1gD421u75J",
        "BV1gD421u75J;send-email",
        "https://www.bilibili.com/video/BV1gD421u75J?p=0",
        "https://www.bilibili.com/video/BV1gD421u75J?p=1&p=2",
        "https://b23.tv/not-resolved",
    ],
)
def test_invalid_reference_never_reaches_network(source):
    with pytest.raises(api().SourceAccessError):
        api().VideoRef.parse(source)


def test_subtitles_are_text_material_not_metadata():
    ref = api().VideoRef.parse("BV1gD421u75J")
    raw = json.dumps([{"index": 1, "from": 1, "to": 2, "content": "source words"}]).encode()
    result = api().parse_reply("subtitle", ref, raw)
    assert result["kind"] == "subtitle"
    assert result["segments"][0]["text"] == "source words"
    assert result["automatically_verified_truth"] is False


def test_observed_cli_seconds_suffix_timestamps_are_parsed():
    # Shape captured verbatim from OpenCLI 1.8.6: from/to are "<seconds>s" strings.
    ref = api().VideoRef.parse("BV1gD421u75J")
    data = [
        {"index": 1, "from": "2.06s", "to": "4.54s", "content": "剪映给视频加字"},
        {"index": 2, "from": "5.82s", "to": "11.64s", "content": "这期教大家用剪映给视频加字幕方法有三种"},
        {"index": 3, "from": "11.64s", "to": "13.16s", "content": "先点赞收藏"},
    ]
    result = api().parse_reply("subtitle", ref, json.dumps(data).encode())
    assert result["kind"] == "subtitle"
    assert [s["start"] for s in result["segments"]] == [2.06, 5.82, 11.64]
    assert result["segments"][0] == {"start": 2.06, "end": 4.54, "text": "剪映给视频加字"}
    assert result["automatically_verified_truth"] is False


@pytest.mark.parametrize(
    "start",
    [
        "2.06",
        "2.06 s",
        " 2.06s",
        "2.06s ",
        "+2.06s",
        "-2.06s",
        "1e3s",
        "abc",
        float("inf"),
        10**500,
        True,
    ],
)
def test_malformed_second_timestamps_are_rejected(start):
    ref = api().VideoRef.parse("BV1gD421u75J")
    data = [{"index": 1, "from": start, "to": "4.54s", "content": "words"}]
    with pytest.raises(api().SourceAccessError):
        api().parse_reply("subtitle", ref, json.dumps(data).encode())


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"field": "title", "value": "not subtitles"}],
        [{"from": 5, "to": 1, "content": "bad time"}],
        [{"from": 0, "to": float("nan"), "content": "bad time"}],
    ],
)
def test_invalid_or_empty_subtitles_do_not_report_success(data):
    with pytest.raises(api().SourceAccessError):
        api().parse_reply(
            "subtitle", api().VideoRef.parse("BV1gD421u75J"), json.dumps(data).encode()
        )


def test_wrong_video_metadata_rejected():
    with pytest.raises(api().SourceAccessError, match="source_mismatch"):
        api().parse_reply(
            "video",
            api().VideoRef.parse("BV1gD421u75J"),
            b'[{"field":"bvid","value":"BV1111111111"}]',
        )


def test_navigation_rejection_is_safe_diagnostic_not_missing_subtitle(tmp_path, monkeypatch):
    import subprocess

    package = tmp_path / "opencli"
    entry = package / "dist/src/main.js"
    entry.parent.mkdir(parents=True)
    (package / "package.json").write_text('{"version":"1.8.6"}')

    def rejected(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, b"", b"Navigation rejected. private details")

    monkeypatch.setattr(subprocess, "run", rejected)
    with pytest.raises(api().SourceAccessError, match="navigation_rejected") as error:
        api().LocalOpenCli("node", entry).fetch("subtitle", api().VideoRef.parse("BV1gD421u75J"))
    assert "private details" not in str(error.value)


@pytest.mark.parametrize("workaround,expected", [(False, "off"), (True, "retain-on-failure")])
def test_navigation_workaround_is_explicit_and_does_not_retry(
    tmp_path, monkeypatch, workaround, expected
):
    import subprocess

    package = tmp_path / "opencli"
    entry = package / "dist/src/main.js"
    entry.parent.mkdir(parents=True)
    (package / "package.json").write_text('{"version":"1.8.6"}')
    calls = []

    def rejected(args, **kwargs):
        calls.append(args)
        assert args[args.index("--trace") + 1] == expected
        assert args[args.index("--keep-tab") + 1] == "false"
        assert not kwargs.get("shell", False)
        return subprocess.CompletedProcess(args, 1, b"", b"Navigation rejected.")

    monkeypatch.setattr(subprocess, "run", rejected)
    adapter = api().LocalOpenCli("node", entry, navigation_workaround=workaround)
    with pytest.raises(api().SourceAccessError, match="navigation_rejected"):
        adapter.fetch("video", api().VideoRef.parse("BV1gD421u75J"))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (b"ok: false\nerror:\n  code: EMPTY_RESULT\n  message: private details\n",
         "subtitle_not_returned"),
        (b'{"ok":false,"error":{"code":"BROWSER_CONNECT","message":"private details"}}',
         "browser_connection_failed"),
        (b"ok: false\nerror:\n  code: AUTH_REQUIRED\n  message: private details\n",
         "source_auth_required"),
        (b"ok: false\nerror:\n  code: FUTURE_ERROR\n  message: private details\n",
         "adapter_failed"),
        (b"page content: code: EMPTY_RESULT private details", "adapter_failed"),
    ],
)
def test_observed_cli_error_envelope_preserves_safe_reason_without_retry(
    tmp_path, monkeypatch, stderr, expected
):
    import subprocess

    entry = tmp_path / "opencli/dist/src/main.js"
    entry.parent.mkdir(parents=True)
    (entry.parents[2] / "package.json").write_text('{"version":"1.8.6"}')
    calls = []

    def failed(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 66, b"", stderr)

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(api().SourceAccessError) as error:
        api().LocalOpenCli("node", entry).fetch("subtitle", api().VideoRef.parse("BV1gD421u75J"))
    assert str(error.value) == expected
    assert len(calls) == 1


def test_empty_subtitle_reply_means_only_that_no_segments_were_returned():
    with pytest.raises(api().SourceAccessError, match="^subtitle_not_returned$"):
        api().parse_reply("subtitle", api().VideoRef.parse("BV1gD421u75J"), b"[]")
