"""Bounded local ASR CLI and isolated worker; only consumes local audio files."""

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_action_agent.local_transcriber import (
    MODELS,
    PARAMETERS,
    PILOT_ROOT,
    SOFTWARE,
    TranscriptionError,
    audio_hash,
    local_audio_path,
    normalize_window,
    transcribe_local,
    validate_result,
)


def checked_candidate(candidate, raw, expected_hash, max_duration, diagnostic_directory):
    diagnostic_directory.mkdir(parents=True, exist_ok=True)
    diagnostic = diagnostic_directory / f"{expected_hash}-{uuid.uuid4().hex}.json"
    details = {
        "validation_error": None,
        "rejected_field": None,
        "candidate": candidate,
        "raw_model_output": raw,
    }
    try:
        result = validate_result(candidate, expected_hash, max_duration)
    except TranscriptionError as error:
        details.update(validation_error=error.code, rejected_field=error.rejected_field)
        diagnostic.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    diagnostic.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def worker(args):
    if not 0 < args.max_duration <= 7200 or not re.fullmatch(
        r"[0-9a-f]{64}", args.expected_sha256 or ""
    ):
        raise TranscriptionError("invalid_worker_result")
    path = local_audio_path(args.audio, 512 * 1024 * 1024)
    if audio_hash(path) != args.expected_sha256:
        raise TranscriptionError("input_changed")
    for package, version in SOFTWARE.items():
        if importlib.metadata.version(package) != version:
            raise TranscriptionError("model_identity_mismatch")
    for name, identity in MODELS.items():
        folder = PILOT_ROOT / "models" / ("SenseVoiceSmall" if name == "asr" else "fsmn-vad")
        manifest_file = folder / "download-manifest.json"
        pinned_manifests = {
            "asr": "ca5ef13e2ae5dff3d9aeb7e8c8d44d4de0f9491a4605b7b04fae85e71825da47",
            "vad": "0dec09b763759b03f93adda93a18e581b3aaf1c3b6be2468f78e791d3ff48402",
        }
        if audio_hash(manifest_file) != pinned_manifests[name]:
            raise TranscriptionError("model_identity_mismatch")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if (
            manifest["model_id"] != identity["id"]
            or manifest["revision"] != identity["revision"]
            or audio_hash(folder / "model.pt") != identity["weight_sha256"]
        ):
            raise TranscriptionError("model_identity_mismatch")
        # Config/tokenizer/normalization are also part of this pinned snapshot.
        for item in manifest["files"]:
            if (
                item["file"]
                in {
                    "config.yaml",
                    "configuration.json",
                    "am.mvn",
                    "chn_jpn_yue_eng_ko_spectok.bpe.model",
                }
                and audio_hash(folder / item["file"]) != item["sha256"]
            ):
                raise TranscriptionError("model_identity_mismatch")
    import imageio_ffmpeg
    import soundfile as sf

    decoder = Path(imageio_ffmpeg.__file__).parent / "binaries/ffmpeg-win-x86_64-v7.1.exe"
    if audio_hash(decoder) != "2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3":
        raise TranscriptionError("model_identity_mismatch")
    formats = {
        ".wav": "wav",
        ".m4a": "mov",
        ".mp3": "mp3",
        ".flac": "flac",
        ".ogg": "ogg",
        ".aac": "aac",
    }
    if path.suffix.lower() not in formats:
        raise TranscriptionError("decode_failed")
    with tempfile.TemporaryDirectory(prefix="decode-", dir=PILOT_ROOT / "tmp") as directory:
        wav = Path(directory) / "audio.wav"
        command = [
            str(decoder),
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            formats[path.suffix.lower()],
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-t",
            str(args.max_duration + 1),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ]
        decoded = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=120
        )
        if decoded.returncode != 0:
            raise TranscriptionError("decode_failed")
        try:
            audio, rate = sf.read(wav, dtype="float32")
        except (sf.LibsndfileError, OSError, ValueError):
            raise TranscriptionError("decode_failed") from None
    duration = len(audio) / rate
    if not duration:
        raise TranscriptionError("empty_audio")
    if duration > args.max_duration:
        raise TranscriptionError("duration_limit")
    asr_input_sha256 = hashlib.sha256(audio.astype("<f4", copy=False).tobytes()).hexdigest()
    import torch
    from funasr import AutoModel

    torch.set_num_threads(PARAMETERS["cpu_threads"])
    model = AutoModel(
        model=str(PILOT_ROOT / "models/SenseVoiceSmall"),
        vad_model=str(PILOT_ROOT / "models/fsmn-vad"),
        device="cpu",
        trust_remote_code=False,
        disable_update=True,
        disable_pbar=True,
        ncpu=PARAMETERS["cpu_threads"],
    )
    run_directory = (
        PILOT_ROOT / "transcription/diagnostics" / f"{args.expected_sha256}-{uuid.uuid4().hex}"
    )
    run_directory.mkdir(parents=True)
    progress = {
        "audio_sha256": args.expected_sha256,
        "asr_input_sha256": asr_input_sha256,
        "parameters": PARAMETERS,
        "total_sample_count": len(audio),
        "boundary_risk": "fixed windows may split words/sentences; not word alignment",
        "status": "running",
        "chunks": [],
    }
    window_samples = PARAMETERS["chunk_seconds"] * rate
    raw_all, segments, chunks = [], [], []
    for index, start_sample in enumerate(range(0, len(audio), window_samples)):
        end_sample = min(start_sample + window_samples, len(audio))
        piece = audio[start_sample:end_sample]
        chunk_hash = hashlib.sha256(piece.astype("<f4", copy=False).tobytes()).hexdigest()
        diagnostic_file = run_directory / f"chunk-{index:04d}.json"
        record = {
            "index": index,
            "start_sample": start_sample,
            "end_sample": end_sample,
            "start": start_sample / rate,
            "end": end_sample / rate,
            "asr_input_sha256": chunk_hash,
            "status": "running",
            "segment_count": 0,
            "asr_seconds": 0.0,
            "diagnostic_file": str(diagnostic_file),
        }
        details = {
            "audio_sha256": args.expected_sha256,
            "asr_input_encoding": "pcm_f32le_16000_mono",
            "asr_input_hash_basis": "actual_worker_array",
            "parameters": PARAMETERS,
            "boundary_risk": progress["boundary_risk"],
            "record": record,
        }
        # Preserve progress even if cancelled inside a model call.
        diagnostic_file.write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        progress["current_chunk"] = index
        (run_directory / "progress.json").write_text(
            json.dumps(progress, indent=2), encoding="utf-8"
        )
        started = time.monotonic()
        try:
            raw = model.generate(
                input=piece,
                fs=rate,
                language=PARAMETERS["language"],
                use_itn=PARAMETERS["use_itn"],
                batch_size_s=PARAMETERS["batch_size_s"],
                merge_vad=PARAMETERS["merge_vad"],
                sentence_timestamp=PARAMETERS["sentence_timestamp"],
            )
            record["asr_seconds"] = time.monotonic() - started
            details["raw_model_output"] = raw
            # Write raw before normalization so a rejection cannot discard it.
            diagnostic_file.write_text(
                json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            normalized = normalize_window(raw, start_sample, end_sample)
        except TranscriptionError as error:
            record["status"] = "rejected"
            details.update(validation_error=error.code, rejected_field=error.rejected_field)
            diagnostic_file.write_text(
                json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            progress.update(status="rejected", rejected_field=error.rejected_field)
            (run_directory / "progress.json").write_text(
                json.dumps(progress, indent=2), encoding="utf-8"
            )
            raise
        record.update(
            status="speech_recognized" if normalized else "empty", segment_count=len(normalized)
        )
        details["segments"] = normalized
        diagnostic_file.write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raw_all.extend(raw)
        segments.extend(normalized)
        chunks.append(record)
        progress["chunks"] = chunks
        (run_directory / "progress.json").write_text(
            json.dumps(progress, indent=2), encoding="utf-8"
        )
    progress["status"] = "all_input_samples_processed"
    (run_directory / "progress.json").write_text(json.dumps(progress, indent=2), encoding="utf-8")
    if not segments:
        raise TranscriptionError("no_speech_recognized")
    if audio_hash(path) != args.expected_sha256:
        raise TranscriptionError("input_changed")
    return checked_candidate(
        {
            "kind": "audio_transcription",
            "audio_sha256": args.expected_sha256,
            "asr_input_sha256": asr_input_sha256,
            "asr_input_encoding": "pcm_f32le_16000_mono",
            "asr_input_hash_basis": "actual_worker_array",
            "duration": duration,
            "models": MODELS,
            "software": SOFTWARE,
            "parameters": PARAMETERS,
            "chunk_manifest": chunks,
            "chunk_coverage": {
                "kind": "all_input_samples_processed",
                "total_sample_count": len(audio),
                "chunk_count": len(chunks),
                "empty_chunk_count": sum(x["status"] == "empty" for x in chunks),
            },
            "boundary_risk": progress["boundary_risk"],
            "timestamp_kind": "vad_segment",
            "time_origin": "local_audio_first_decoded_sample",
            "segments": segments,
            "automatically_verified_truth": False,
        },
        raw_all,
        args.expected_sha256,
        args.max_duration,
        PILOT_ROOT / "transcription/diagnostics",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-duration", type=float, default=3600)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expected-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        result = (
            worker(args)
            if args.worker
            else transcribe_local(
                args.audio, max_duration=args.max_duration, timeout_seconds=args.timeout
            )
        )
    except TranscriptionError as error:
        result = {"error": error.code}
        if error.rejected_field:
            result["rejected_field"] = error.rejected_field
    except subprocess.TimeoutExpired:
        result = {"error": "transcription_timeout"}
    except Exception:  # noqa: BLE001 - CLI boundary exposes no unstructured dependency exceptions.
        result = {"error": "transcription_failed"}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
