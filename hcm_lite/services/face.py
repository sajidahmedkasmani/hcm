"""Server-side face verification for camera attendance.

The browser only captures frames; every decision is made here:
  * exactly one face in view (no face / several faces are rejected),
  * the face matches the employee's enrolled face (128-d dlib ResNet descriptor, Euclidean distance),
  * optional liveness: a blink seen across a short burst of frames (eye aspect ratio dip).

Models ship inside pip packages (`dlib-bin`, `face_recognition_models`), so nothing downloads at runtime.
Frames are decoded in memory and discarded; only the enrolled descriptor (128 numbers, not a photo) and
the verification result are stored.
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import io
import os
import statistics
import threading
from dataclasses import dataclass
from typing import Any

from ..errors import DomainError, ValidationFailed

MAX_FRAME_BYTES = 400_000
MAX_SIDE = 640
ENROLL_CONSISTENCY = 0.45  # max distance between enrollment frames of the same sitting


class FaceEngineUnavailable(DomainError):
    status_code, code = 503, "face_engine_unavailable"


@dataclass(frozen=True)
class FrameAnalysis:
    faces: int
    face_px: int = 0                       # width of the largest face, a quality signal
    descriptor: tuple[float, ...] | None = None
    ear: float | None = None               # mean eye aspect ratio of the largest face


def decode_frame(data: str | bytes) -> Any:
    """Base64 (optionally a data: URL) or raw bytes -> RGB numpy array, longest side <= 640 px."""
    import numpy as np
    from PIL import Image, UnidentifiedImageError

    if isinstance(data, str):
        payload = data.split(",", 1)[1] if data.startswith("data:") else data
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationFailed("A camera frame was not valid base64 image data", code="bad_frame") from exc
    else:
        raw = data
    if len(raw) > MAX_FRAME_BYTES:
        raise ValidationFailed("A camera frame is too large (max 400 KB)", code="bad_frame")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationFailed("A camera frame could not be read as an image", code="bad_frame") from exc
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    return np.asarray(img)


def eye_aspect_ratio(points: list[tuple[float, float]]) -> float:
    """EAR = (|p2-p6| + |p3-p5|) / (2 |p1-p4|) for the six landmarks of one eye (Soukupová & Čech, 2016)."""
    import numpy as np

    p = np.asarray(points, dtype=float)
    return float((np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])) / (2 * np.linalg.norm(p[0] - p[3])))


def blink_detected(ears: list[float], *, min_open: float = 0.18, dip: float = 0.70) -> bool:
    """A blink = eyes clearly open in some frames and at least 30% more closed in another.
    Relative, so naturally narrow eyes still work; a printed photo can't produce the dip."""
    values = [e for e in ears if e is not None]
    if len(values) < 3:
        return False
    open_level = statistics.median(sorted(values)[len(values) // 2:])  # typical open-eye value
    return open_level >= min_open and min(values) <= dip * open_level


def distance(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> float:
    import numpy as np

    return float(np.linalg.norm(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def mean_descriptor(descriptors: list[tuple[float, ...]]) -> list[float]:
    import numpy as np

    return [round(float(x), 6) for x in np.mean(np.asarray(descriptors, dtype=float), axis=0)]


def _bundled_models_dir() -> str | None:
    """Model files shipped by face_recognition_models, found without importing it (its __init__ uses the
    deprecated pkg_resources)."""
    spec = importlib.util.find_spec("face_recognition_models")
    if spec is None or not spec.submodule_search_locations:
        return None
    return os.path.join(next(iter(spec.submodule_search_locations)), "models")


class DlibFaceEngine:
    """HOG face detector + 68-point landmarks + ResNet descriptor (dlib). Loaded once, used under a lock."""

    def __init__(self, models_dir: str | None = None) -> None:
        self._models_dir = models_dir
        self._lock = threading.Lock()
        self._loaded = False

    def _load(self) -> None:
        missing = "Face verification isn't installed on this server (pip install dlib-bin face_recognition_models)"
        try:
            import dlib
        except ImportError as exc:
            raise FaceEngineUnavailable(missing) from exc
        models = self._models_dir or _bundled_models_dir()
        if models is None:
            raise FaceEngineUnavailable(missing)
        self._detector = dlib.get_frontal_face_detector()
        self._landmarks = dlib.shape_predictor(os.path.join(models, "shape_predictor_68_face_landmarks.dat"))
        self._encoder = dlib.face_recognition_model_v1(os.path.join(models, "dlib_face_recognition_resnet_model_v1.dat"))
        self._loaded = True

    def analyze(self, image: Any, *, descriptor: bool = True) -> FrameAnalysis:
        with self._lock:
            if not self._loaded:
                self._load()
            faces = self._detector(image, 0)
            if len(faces) == 0:  # small or distant face: one upsampling pass (only when nothing was found)
                faces = self._detector(image, 1)
            if len(faces) != 1:
                return FrameAnalysis(faces=len(faces))
            box = faces[0]
            shape = self._landmarks(image, box)
            pts = [(shape.part(i).x, shape.part(i).y) for i in range(68)]
            ear = (eye_aspect_ratio(pts[36:42]) + eye_aspect_ratio(pts[42:48])) / 2
            desc = tuple(self._encoder.compute_face_descriptor(image, shape)) if descriptor else None
            return FrameAnalysis(faces=1, face_px=box.width(), descriptor=desc, ear=ear)


_ENGINE: DlibFaceEngine | None = None


def get_engine() -> DlibFaceEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DlibFaceEngine()
    return _ENGINE


@dataclass(frozen=True)
class Verification:
    frames: int
    frames_with_face: int
    descriptor: tuple[float, ...] | None
    best_descriptors: list[tuple[float, ...]]
    ears: list[float]


def inspect_frames(frames: list[str | bytes], *, max_descriptors: int = 3) -> Verification:
    """Run detection on every frame; descriptors only on the clearest few (largest faces)."""
    engine = get_engine()
    images = [decode_frame(f) for f in frames]
    analyses = [engine.analyze(img, descriptor=False) for img in images]
    if any(a.faces > 1 for a in analyses):
        raise ValidationFailed("More than one face is in view — only the person checking in should be in the frame",
                               code="multiple_faces")
    with_face = [(a, img) for a, img in zip(analyses, images) if a.faces == 1]
    if not with_face:
        raise ValidationFailed("No face detected — face the camera in good light", code="no_face")
    clearest = sorted(with_face, key=lambda pair: -pair[0].face_px)[:max_descriptors]
    descriptors = [engine.analyze(img, descriptor=True).descriptor for _, img in clearest]
    descriptors = [d for d in descriptors if d is not None]
    return Verification(frames=len(frames), frames_with_face=len(with_face),
                        descriptor=descriptors[0] if descriptors else None, best_descriptors=descriptors,
                        ears=[a.ear for a, _ in with_face if a.ear is not None])
