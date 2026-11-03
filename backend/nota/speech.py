"""Local speech stack: Silero VAD (speech time, also what minutes are charged on), SenseVoice-Small
(transcription, via FunASR) and CAM++ (speaker embeddings). Models load once per process."""
import os
from pathlib import Path

import numpy as np

from nota import config

RATE = 16000
MERGE_GAP = 0.8      # VAD pieces closer than this form one utterance
MAX_UTTERANCE = 25   # seconds; SenseVoice is most accurate on short inputs
MIN_EMBED = 0.8      # shorter utterances give unreliable embeddings
_models = {}


def _model_ref(model_id: str) -> str:
    cached = Path(os.environ.get("MODELSCOPE_CACHE", Path.home() / ".cache/modelscope")) / "models" / model_id.replace("/", "--") / "snapshots/master"
    return str(cached) if cached.exists() else model_id


def load():
    if _models:
        return
    import torch
    from funasr import AutoModel
    from silero_vad import load_silero_vad

    torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "2")))
    common = {"disable_update": True, "device": "cpu", "hub": "ms", "log_level": "ERROR"}
    _models["asr"] = AutoModel(model=_model_ref("iic/SenseVoiceSmall"), **common)
    _models["sv"] = AutoModel(model=_model_ref("iic/speech_campplus_sv_zh-cn_16k-common"), **common)
    _models["vad"] = load_silero_vad()


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768


def vad(samples: np.ndarray) -> list[tuple[float, float]]:
    import torch
    from silero_vad import get_speech_timestamps

    load()
    ts = get_speech_timestamps(torch.from_numpy(samples), _models["vad"], sampling_rate=RATE, return_seconds=True)
    return [(t["start"], t["end"]) for t in ts]


def utterances(segments: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in segments:
        if merged and start - merged[-1][1] < MERGE_GAP and end - merged[-1][0] <= MAX_UTTERANCE:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def transcribe(samples: np.ndarray) -> str:
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    load()
    out = _models["asr"].generate(input=samples, language="auto", use_itn=True, disable_pbar=True)
    return rich_transcription_postprocess(out[0]["text"]).strip()


def embed(samples: np.ndarray) -> np.ndarray:
    load()
    vec = _models["sv"].generate(input=samples, disable_pbar=True)[0]["spk_embedding"].reshape(-1).numpy()
    return vec / np.linalg.norm(vec)


def cosine(a, b) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def assign_clusters(embeddings: list[np.ndarray | None], clusters: list[dict], previous: int | None) -> tuple[list[int | None], list[dict]]:
    """Greedy online clustering, continued across batches of the same recording. clusters is the
    persisted state [{"sum": [...], "n": k}]; an utterance too short to embed joins the previous speaker."""
    state = [{"sum": np.array(c["sum"], dtype=np.float32), "n": c["n"]} for c in clusters]
    out: list[int | None] = []
    for e in embeddings:
        if e is None:
            out.append(previous)
            continue
        scores = [cosine(e, c["sum"]) for c in state]
        if scores and max(scores) >= config.SPEAKER_CLUSTER_THRESHOLD:
            k = int(np.argmax(scores))
            state[k]["sum"] += e
            state[k]["n"] += 1
        else:
            state.append({"sum": e.astype(np.float32).copy(), "n": 1})
            k = len(state) - 1
        out.append(k)
        previous = k
    return out, [{"sum": c["sum"].tolist(), "n": c["n"]} for c in state]


def cluster_names(clusters: list[dict], owner: np.ndarray | None) -> dict[int, str]:
    """The cluster closest to the enrolled voiceprint (above the threshold) is "我"; the others are
    A, B, C… in order of first appearance. Without a voiceprint nobody is marked."""
    owner_k = None
    if owner is not None and clusters:
        scores = [cosine(owner, np.array(c["sum"])) for c in clusters]
        if max(scores) >= config.OWNER_THRESHOLD:
            owner_k = int(np.argmax(scores))
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return {k: "我" if k == owner_k else next(letters) for k in range(len(clusters))}


def transcribe_spans(samples: np.ndarray, offset: float, spans: list[tuple[float, float]]) -> list[dict]:
    """ASR and an embedding per utterance; samples start at `offset` seconds of the recording."""
    out = []
    for start, end in spans:
        piece = samples[int((start - offset) * RATE) : int((end - offset) * RATE)]
        text = transcribe(piece)
        if text:
            out.append({"start": round(start, 2), "end": round(end, 2), "text": text, "embedding": embed(piece) if end - start >= MIN_EMBED else None})
    return out


def speech_seconds(segments: list[tuple[float, float]]) -> float:
    return round(sum(e - s for s, e in segments), 2)


def enrollment_embedding(samples: np.ndarray, min_speech: float = 6.0) -> np.ndarray:
    """Voiceprint from a reading sample; raises ValueError with a user-facing reason when unusable."""
    spans = vad(samples)
    speech = sum(e - s for s, e in spans)
    if speech < min_speech:
        raise ValueError(f"有效说话时长 {speech:.1f} 秒，太短，请读 10 秒以上")
    if speech / (len(samples) / RATE) < 0.3:
        raise ValueError("录音里说话太少或噪声太大，请在安静处重读")
    voiced = np.concatenate([samples[int(s * RATE) : int(e * RATE)] for s, e in spans])
    return embed(voiced)
