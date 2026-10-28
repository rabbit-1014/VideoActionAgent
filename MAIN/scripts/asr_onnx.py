"""Pinned, offline ONNX worker with bounded waveform buffers."""
import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_action_agent.asr_worker_resources import memory_metrics, start_guard
from video_action_agent.local_transcriber import (
    PILOT_ROOT,
    TranscriptionError,
    audio_hash,
    local_audio_path,
    validate_result,
)
from video_action_agent.onnx_profile import MODELS, PARAMETERS, ROOT, SOFTWARE, TIMESTAMP_KIND
from video_action_agent.onnx_transcriber import speech_present


def worker(args):
    if not 0 < args.max_duration <= 7200:
        raise TranscriptionError("duration_limit")
    path = local_audio_path(args.audio, 512 * 1024 * 1024)
    if audio_hash(path) != args.expected_sha256:
        raise TranscriptionError("input_changed")
    for package, version in SOFTWARE.items():
        if importlib.metadata.version(package) != version:
            raise TranscriptionError("model_identity_mismatch")
    model_dir = ROOT / "model"
    for name, expected in (("model.int8.onnx", MODELS["asr"]["weight_sha256"]),
                           ("tokens.txt", MODELS["asr"]["tokens_sha256"]),
                           ("silero_vad.onnx", MODELS["vad"]["weight_sha256"])):
        if audio_hash(model_dir / name) != expected:
            raise TranscriptionError("model_identity_mismatch")
    decoder = PILOT_ROOT / "venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
    if audio_hash(decoder) != "2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3":
        raise TranscriptionError("model_identity_mismatch")
    formats = {".wav": "wav", ".m4a": "mov", ".mp3": "mp3", ".flac": "flac",
               ".ogg": "ogg", ".aac": "aac"}
    if path.suffix.lower() not in formats:
        raise TranscriptionError("decode_failed")
    import sherpa_onnx
    import soundfile

    run = ROOT / "diagnostics" / (args.expected_sha256 + "-" + uuid.uuid4().hex)
    run.mkdir(parents=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="decode-", dir=ROOT) as directory:
        wav = Path(directory) / "audio.wav"
        decoded = subprocess.run([
            str(decoder), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-f", formats[path.suffix.lower()], "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-t", str(args.max_duration + 1), "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(wav),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False)
        if decoded.returncode:
            raise TranscriptionError("decode_failed")
        with soundfile.SoundFile(wav) as reader:
            rate, total = reader.samplerate, len(reader)
            duration = total / rate
            if not total:
                raise TranscriptionError("empty_audio")
            if rate != 16000 or reader.channels != 1:
                raise TranscriptionError("decode_failed")
            if duration > args.max_duration:
                raise TranscriptionError("duration_limit")
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = str(model_dir / "silero_vad.onnx")
            for name in ("threshold", "min_silence_duration", "min_speech_duration", "max_speech_duration", "window_size"):
                setattr(config.silero_vad, name, PARAMETERS["vad"][name])
            config.sample_rate, config.num_threads, config.provider = rate, PARAMETERS["vad"]["num_threads"], "cpu"
            detector = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
            recognizer, load_seconds = None, 0.0
            pcm = hashlib.sha256()
            segments, chunks = [], []
            window = PARAMETERS["chunk_seconds"] * rate
            for index, start in enumerate(range(0, total, window)):
                piece = reader.read(min(window, total - start), dtype="float32").astype("<f4", copy=False)
                end = start + len(piece)
                if end != min(start + window, total):
                    raise TranscriptionError("decode_failed")
                block = memoryview(piece).cast("B")
                pcm.update(block)
                begin = time.monotonic()
                speech = speech_present(detector, piece, frame_size=PARAMETERS["vad"]["window_size"])
                text = ""
                if speech:
                    if recognizer is None:
                        load = time.monotonic()
                        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                            model=str(model_dir / "model.int8.onnx"), tokens=str(model_dir / "tokens.txt"),
                            num_threads=PARAMETERS["cpu_threads"], language=PARAMETERS["language"],
                            use_itn=PARAMETERS["use_itn"], decoding_method=PARAMETERS["decoding_method"], provider="cpu",
                        )
                        load_seconds = time.monotonic() - load
                    stream = recognizer.create_stream()
                    stream.accept_waveform(rate, piece)
                    recognizer.decode_stream(stream)
                    text = stream.result.text.strip()
                if text:
                    segments.append({"start": start / rate, "end": end / rate, "text": text})
                record = {"index": index, "start_sample": start, "end_sample": end,
                    "start": start / rate, "end": end / rate,
                    "asr_input_sha256": hashlib.sha256(block).hexdigest(),
                    "status": "speech_recognized" if text else "empty", "segment_count": int(bool(text)),
                    "asr_seconds": time.monotonic() - begin,
                    "diagnostic_file": str(run / f"chunk-{index:04d}.json")}
                chunks.append(record)
                Path(record["diagnostic_file"]).write_text(json.dumps({"record": record, "text": text, "speech_detected": speech}, ensure_ascii=False), encoding="utf-8")
                (run / "progress.json").write_text(json.dumps({"status": "running", "audio_sha256": args.expected_sha256,
                    "processed_samples": end, "total_samples": total, "completed_chunks": len(chunks)}), encoding="utf-8")
            if not segments:
                raise TranscriptionError("no_speech_recognized")
    result = {"kind": "audio_transcription", "audio_sha256": args.expected_sha256,
        "asr_input_sha256": pcm.hexdigest(), "asr_input_encoding": "pcm_f32le_16000_mono",
        "asr_input_hash_basis": "actual_worker_array", "duration": duration,
        "models": MODELS, "software": SOFTWARE, "parameters": PARAMETERS,
        "timestamp_kind": TIMESTAMP_KIND, "time_origin": "local_audio_first_decoded_sample",
        "automatically_verified_truth": False, "segments": segments, "chunk_manifest": chunks,
        "chunk_coverage": {"kind": "all_input_samples_processed", "total_sample_count": total,
            "chunk_count": len(chunks), "empty_chunk_count": sum(not c["segment_count"] for c in chunks)},
        "boundary_risk": "Fixed windows may split words; times bound the audio window, not words or VAD segments.",
        "runtime_metrics": {"model_load_seconds": load_seconds, "total_worker_seconds": time.monotonic() - started,
                            **memory_metrics()}}
    validate_result(result, args.expected_sha256, args.max_duration)
    (run / "progress.json").write_text(json.dumps({"status": "all_input_samples_processed", "audio_sha256": args.expected_sha256,
        "processed_samples": total, "total_samples": total, "completed_chunks": len(chunks)}), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-duration", type=float, required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    guard_dir = ROOT / "diagnostics"
    guard_dir.mkdir(parents=True, exist_ok=True)
    done = start_guard(args.output, guard_dir / ("resource-" + uuid.uuid4().hex + ".json"))
    try:
        result = worker(args)
    except TranscriptionError as exc:
        result = {"error": exc.code}
    except subprocess.TimeoutExpired:
        result = {"error": "transcription_timeout"}
    except Exception:  # noqa: BLE001 -- expose only stable worker errors, never dependency details.
        result = {"error": "transcription_failed"}
    finally:
        done.set()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return int("error" in result)


if __name__ == "__main__":
    sys.exit(main())
