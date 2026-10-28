"""Alternative worker under the same cancellation and input identity fences."""
from pathlib import Path

from .local_transcriber import transcribe_local
from .onnx_profile import ROOT


def speech_present(detector, samples, *, frame_size=512):
    """Only decide presence; stop after a positive, but scan/flush all negatives."""
    detector.reset()
    for start in range(0, len(samples), frame_size):
        detector.accept_waveform(samples[start:start + frame_size])
        if detector.is_speech_detected() or not detector.empty():
            return True
    detector.flush()
    return not detector.empty()


def transcribe_onnx(audio_path, **options):
    return transcribe_local(
        audio_path, python_path=ROOT / "venv/Scripts/python.exe",
        worker_script=Path(__file__).resolve().parents[2] / "scripts/asr_onnx.py",
        **options,
    )
