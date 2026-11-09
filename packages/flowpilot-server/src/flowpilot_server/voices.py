"""Voice profiles of the household and telling who speaks (``flowpilot_sdk.Voices``).

A profile is a person's speech samples turned into embeddings by a speaker model (sherpa-onnx);
the audio itself is never kept. Kept in ``voices.json`` next to the runtime's config (mode 600,
in Home Assistant backups, as the owner chose). The models (~28 MB each) are downloaded on demand
into the runtime's data, checked against their known hash.

A turn's speech matches the person whose profile (the mean of their samples) is closest by
cosine similarity, when that is at least the threshold and ahead of the next person by the
margin; otherwise the speaker is unknown. Scores of different models are not comparable, so
each model has its own default threshold, tuned on the household's own voices in the editor.
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from flowpilot_sdk import SpeakerMatch, SpeakerStatus, new_ulid

_LOGGER = logging.getLogger(__name__)

MODELS_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
MIN_SECONDS = 1.0  # less speech than this says too little about a voice
MAX_SECONDS = 10.0  # of one sample or one turn: more adds little
QUIET_DB = -45.0  # an RMS level below this is (almost) silence
MAX_SAMPLES = 20  # per person


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    title: str
    file: str
    sha256: str
    # a cautious start (an unknown voice taken for someone is worse than a known one missed);
    # tune it on the household's voices in the editor
    threshold: float


MODELS = {
    m.id: m
    for m in (
        Model(
            "wespeaker-resnet34",
            "WeSpeaker ResNet34",
            "wespeaker_en_voxceleb_resnet34.onnx",
            "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94",
            0.8,
        ),
        Model(
            "3dspeaker-campplus",
            "3D-Speaker CAM++",
            "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
            "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b",
            0.65,
        ),
    )
}
DEFAULT_MODEL = "wespeaker-resnet34"
DEFAULT_MARGIN = 0.05


def pcm_floats(pcm: bytes) -> list[float]:
    """s16le mono as floats in [-1, 1]."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    return [s / 32768 for s in samples]


def level_db(samples: list[float]) -> float:
    if not samples:
        return -120.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(rms) if rms > 0 else -120.0


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def mean_vector(vectors: list[list[float]]) -> list[float]:
    """The mean of unit vectors, as a unit vector: a person's profile."""
    units = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        units.append([x / norm for x in v])
    mean = [sum(column) / len(units) for column in zip(*units, strict=True)]
    norm = math.sqrt(sum(x * x for x in mean)) or 1.0
    return [x / norm for x in mean]


class VoiceError(Exception):
    """A sample cannot be used, or the model is missing."""


class _Embedder:
    """One speaker model, loaded on first use; it computes in a thread (onnxruntime frees the
    GIL)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._extractor: Any = None
        self._lock = asyncio.Lock()

    async def embed(self, samples: list[float], sample_rate: int) -> list[float]:
        async with self._lock:  # one compute at a time: the Pi has few cores to spare
            return await asyncio.to_thread(self._embed, samples, sample_rate)

    def _embed(self, samples: list[float], sample_rate: int) -> list[float]:
        if self._extractor is None:
            import sherpa_onnx  # type: ignore[import-untyped]  # noqa: PLC0415 - heavy, on use

            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.path), num_threads=2
            )
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate, samples)  # resampled to the model's rate
        stream.input_finished()
        return [float(x) for x in self._extractor.compute(stream)]


class VoiceStore:
    """Profiles in ``path`` (mode 600; in memory when ``None``), models in ``models_dir``."""

    def __init__(self, path: Path | None, models_dir: Path | None) -> None:
        self.path = path
        self.models_dir = models_dir
        self._data: dict[str, Any] = {
            "model": DEFAULT_MODEL,
            "margin": DEFAULT_MARGIN,
            "people": {},
        }
        self._embedders: dict[str, _Embedder] = {}
        self._downloads: dict[str, asyncio.Task[None]] = {}
        if path is not None and path.exists():
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                self._data.update(loaded)

    # ---- settings ---------------------------------------------------------------------------

    @property
    def model(self) -> Model:
        return MODELS.get(str(self._data.get("model")), MODELS[DEFAULT_MODEL])

    @property
    def threshold(self) -> float:
        value = self._data.get("threshold")
        return float(value) if isinstance(value, int | float) else self.model.threshold

    @property
    def margin(self) -> float:
        return float(self._data.get("margin", DEFAULT_MARGIN))

    def settings(self) -> dict[str, Any]:
        return {
            "model": self.model.id,
            "threshold": self.threshold,
            "default_threshold": self.model.threshold,
            "margin": self.margin,
            "models": [
                {"id": m.id, "title": m.title, "ready": self.ready(m.id)} for m in MODELS.values()
            ],
        }

    def set_settings(self, data: Mapping[str, Any]) -> None:
        if "model" in data:
            if data["model"] not in MODELS:
                raise VoiceError(f"no voice model {data['model']!r}")
            if data["model"] != self._data.get("model"):
                self._data.pop("threshold", None)  # another model, another scale
            self._data["model"] = data["model"]
        if "threshold" in data:
            if data["threshold"] is None:
                self._data.pop("threshold", None)
            else:
                self._data["threshold"] = _bounded(data["threshold"], -1.0, 1.0, "threshold")
        if "margin" in data:
            self._data["margin"] = _bounded(data["margin"], 0.0, 1.0, "margin")
        self._save()

    # ---- models -----------------------------------------------------------------------------

    def _model_path(self, model: Model) -> Path | None:
        return self.models_dir / model.file if self.models_dir is not None else None

    def ready(self, model_id: str) -> bool:
        path = self._model_path(MODELS[model_id])
        return path is not None and path.is_file()

    async def download(self, model_id: str) -> None:
        """Fetch a model (once at a time), checked against its hash."""
        model = MODELS[model_id]
        path = self._model_path(model)
        if path is None:
            raise VoiceError("this runtime has no data directory for voice models")
        if path.is_file():
            return
        task = self._downloads.get(model_id)
        if task is None or task.done():
            task = asyncio.create_task(self._fetch(model, path))
            self._downloads[model_id] = task
        await asyncio.shield(task)

    @staticmethod
    async def _fetch(model: Model, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        digest = hashlib.sha256()
        timeout = aiohttp.ClientTimeout(total=600, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{MODELS_URL}/{model.file}") as resp:
                if resp.status != 200:
                    raise VoiceError(f"cannot download {model.title}: HTTP {resp.status}")
                with part.open("wb") as out:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        digest.update(chunk)
                        out.write(chunk)
        if digest.hexdigest() != model.sha256:
            part.unlink(missing_ok=True)
            raise VoiceError(f"{model.title} did not download intact (hash mismatch)")
        part.replace(path)
        _LOGGER.info("voice model %s downloaded", model.id)

    async def _embed(self, samples: list[float], sample_rate: int) -> list[float]:
        model = self.model
        path = self._model_path(model)
        if path is None or not path.is_file():
            raise VoiceError(f"the voice model {model.title} is not downloaded")
        embedder = self._embedders.get(model.id)
        if embedder is None:
            embedder = self._embedders[model.id] = _Embedder(path)
        return await embedder.embed(samples, sample_rate)

    # ---- profiles ---------------------------------------------------------------------------

    def people(self) -> dict[str, list[dict[str, Any]]]:
        """Each person's samples (no embeddings), the ones of the current model marked."""
        out: dict[str, list[dict[str, Any]]] = {}
        for user_id, person in self._data.get("people", {}).items():
            out[user_id] = [
                {k: v for k, v in s.items() if k != "embedding"}
                | {"current": s.get("model") == self.model.id}
                for s in person.get("samples", [])
            ]
        return out

    async def add_sample(
        self, user_id: str, pcm: bytes, sample_rate: int, *, source: str
    ) -> dict[str, Any]:
        """A sample of a person's speech: checked, turned into an embedding and kept (not the
        audio)."""
        if not user_id:
            raise VoiceError("whose voice is it?")
        samples = pcm_floats(pcm)[: int(MAX_SECONDS * sample_rate)]
        seconds = len(samples) / sample_rate
        if seconds < MIN_SECONDS:
            raise VoiceError(f"too short: {seconds:.1f} s, at least {MIN_SECONDS:.0f} s of speech")
        level = level_db(samples)
        if level < QUIET_DB:
            raise VoiceError(f"too quiet ({level:.0f} dB): speak closer or louder")
        person = self._data.setdefault("people", {}).setdefault(user_id, {"samples": []})
        if len(person["samples"]) >= MAX_SAMPLES:
            raise VoiceError(f"{MAX_SAMPLES} samples is enough: remove some first")
        embedding = await self._embed(samples, sample_rate)
        sample = {
            "id": new_ulid(),
            "model": self.model.id,
            "source": source,
            "seconds": round(seconds, 1),
            "level_db": round(level, 1),
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "embedding": [round(x, 6) for x in embedding],
        }
        person["samples"].append(sample)
        self._save()
        return {k: v for k, v in sample.items() if k != "embedding"} | {"current": True}

    def remove_sample(self, user_id: str, sample_id: str) -> bool:
        person = self._data.get("people", {}).get(user_id)
        if not person:
            return False
        kept = [s for s in person["samples"] if s.get("id") != sample_id]
        if len(kept) == len(person["samples"]):
            return False
        person["samples"] = kept
        if not kept:
            del self._data["people"][user_id]
        self._save()
        return True

    def remove_person(self, user_id: str) -> bool:
        if self._data.get("people", {}).pop(user_id, None) is None:
            return False
        self._save()
        return True

    def _profiles(self) -> dict[str, list[float]]:
        """Each person's profile for the current model (people without samples of it left out)."""
        out = {}
        for user_id, person in self._data.get("people", {}).items():
            vectors = [
                s["embedding"] for s in person.get("samples", []) if s.get("model") == self.model.id
            ]
            if vectors:
                out[user_id] = mean_vector(vectors)
        return out

    async def scores(self, pcm: bytes, sample_rate: int) -> dict[str, float]:
        """How close this speech is to each person's profile."""
        samples = pcm_floats(pcm)[: int(MAX_SECONDS * sample_rate)]
        if len(samples) / sample_rate < MIN_SECONDS:
            raise VoiceError("too little speech to tell")
        profiles = self._profiles()
        if not profiles:
            raise VoiceError("nobody has a voice profile yet")
        embedding = await self._embed(samples, sample_rate)
        return {user_id: round(cosine(embedding, p), 4) for user_id, p in profiles.items()}

    async def identify(self, pcm: bytes, sample_rate: int) -> SpeakerMatch:
        model = self.model
        try:
            scores = await self.scores(pcm, sample_rate)
        except VoiceError as err:
            return SpeakerMatch(SpeakerStatus.ERROR, reason=str(err), model_id=model.id)
        ranked = sorted(scores.items(), key=lambda item: -item[1])
        best_id, best = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        if best < self.threshold:
            reason = "not close enough to anyone"
        elif best - second < self.margin:
            reason = "too close to two people"
        else:
            return SpeakerMatch(SpeakerStatus.MATCHED, best_id, best, self.threshold, model.id)
        return SpeakerMatch(
            SpeakerStatus.UNKNOWN, None, best, self.threshold, model.id, reason=reason
        )

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump({"version": 1, **self._data}, out, ensure_ascii=False)
        tmp.replace(self.path)


def _bounded(value: Any, low: float, high: float, name: str) -> float:
    try:
        number = float(value)
    except TypeError, ValueError:
        raise VoiceError(f"{name} must be a number") from None
    if not low <= number <= high:
        raise VoiceError(f"{name} must be between {low} and {high}")
    return number
