"""Camera frames for tests, built from real face photos (see fixtures/faces/README.md)."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageEnhance

FACES = Path(__file__).parent / "fixtures" / "faces"
OFFICE = (24.8607, 67.0011)  # matches the office location set in conftest's company fixture


def _b64(img: Image.Image, data_url: bool = False) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    encoded = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{encoded}" if data_url else encoded


def _person(name: str) -> Image.Image:
    return Image.open(FACES / f"person_{name}.jpg").convert("RGB")


_VARIANTS = [
    lambda i: ImageEnhance.Brightness(i).enhance(0.8),
    lambda i: i.rotate(3),
    lambda i: i.crop((10, 6, 312, 326)).resize(i.size),
    lambda i: ImageEnhance.Brightness(i).enhance(1.15),
    lambda i: ImageEnhance.Contrast(i).enhance(0.85),
]


def frames_of(person: str, n: int = 3) -> list[str]:
    """n slightly different 'captures' of the same person (like consecutive webcam frames)."""
    base = _person(person)
    return [_b64(_VARIANTS[k % len(_VARIANTS)](base), data_url=k == 0) for k in range(n)]


def blink_frames() -> list[str]:
    a, closed = _person("a"), Image.open(FACES / "person_a_eyes_closed.jpg")
    return [_b64(a), _b64(_VARIANTS[0](a)), _b64(closed), _b64(_VARIANTS[1](a))]


def two_faces() -> list[str]:
    a, b = _person("a"), _person("b").resize((320, 332))
    canvas = Image.new("RGB", (660, 340), (200, 200, 200))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (335, 0))
    return [_b64(canvas)] * 3


def no_face() -> list[str]:
    img = Image.linear_gradient("L").resize((320, 240)).convert("RGB")
    return [_b64(img)] * 3


def payload(action: str, frames: list[str], *, lat: float = OFFICE[0], lng: float = OFFICE[1],
            accuracy_m: float = 15.0) -> dict:
    return {"action": action, "frames": frames, "lat": lat, "lng": lng, "accuracy_m": accuracy_m}
