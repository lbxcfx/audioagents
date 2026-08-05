#!/usr/bin/env python3
from __future__ import annotations

import argparse
import audioop
from pathlib import Path
import wave

import av
import numpy as np


ROOT = Path(__file__).resolve().parents[2]


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("input must be 16-bit mono PCM WAV")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").copy(), source.getframerate()


def resample(samples: np.ndarray, input_rate: int, output_rate: int) -> np.ndarray:
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(samples))
    frame.sample_rate = input_rate
    frame.planes[0].update(samples.astype("<i2", copy=False).tobytes())
    converter = av.AudioResampler(format="s16", layout="mono", rate=output_rate)
    frames = converter.resample(frame) + converter.resample(None)
    return np.concatenate(
        [np.frombuffer(bytes(item.planes[0]), dtype="<i2")[: item.samples] for item in frames]
    ).copy()


def telephone_master(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Prepare wideband synthetic speech for a single narrowband encode."""

    signal = samples.astype(np.float64) / 32768.0
    # Remove sub-telephone-band energy that consumes headroom but is inaudible
    # after G.711.  This one-pole high pass has a corner near 150 Hz at 24 kHz.
    coefficient = np.exp(-2.0 * np.pi * 150.0 / sample_rate)
    filtered = np.empty_like(signal)
    previous_input = previous_output = 0.0
    for index, value in enumerate(signal):
        previous_output = coefficient * (previous_output + value - previous_input)
        previous_input = value
        filtered[index] = previous_output

    # Gentle presence emphasis.  The following high-pass residual is mixed
    # back before the anti-aliasing resampler, concentrating intelligibility
    # in the 1.5-3.4 kHz band retained by PCMU.
    smoothing = max(1, round(sample_rate / 1_600))
    kernel = np.ones(smoothing) / smoothing
    presence = filtered - np.convolve(filtered, kernel, mode="same")
    filtered += 0.22 * presence

    # Soft compression followed by a -2 dBFS ceiling prevents synthetic
    # peaks from wasting G.711's limited quantization range.
    filtered = np.tanh(filtered * 1.35) / np.tanh(1.35)
    peak = np.max(np.abs(filtered)) or 1.0
    filtered *= min(1.0, (10.0 ** (-2.0 / 20.0)) / peak)
    return np.round(np.clip(filtered, -1.0, 1.0) * 32767.0).astype("<i2")


def pcmu_roundtrip(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    narrow = resample(samples, sample_rate, 8_000)
    encoded = audioop.lin2ulaw(narrow.tobytes(), 2)
    decoded = np.frombuffer(audioop.ulaw2lin(encoded, 2), dtype="<i2").copy()
    return resample(decoded, 8_000, sample_rate)


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.astype("<i2", copy=False).tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=ROOT / "qwen-telephony/artifacts/audio-comparison/qwen-realtime-direct.wav",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "qwen-telephony/artifacts/audio-comparison",
    )
    args = parser.parse_args()
    samples, sample_rate = read_wav(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_wav(
        args.output_dir / "qwen-pcmu-baseline.wav",
        pcmu_roundtrip(samples, sample_rate),
        sample_rate,
    )
    write_wav(
        args.output_dir / "qwen-pcmu-mastered.wav",
        pcmu_roundtrip(telephone_master(samples, sample_rate), sample_rate),
        sample_rate,
    )


if __name__ == "__main__":
    main()
