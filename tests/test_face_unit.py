"""Face-verification helpers that don't need dlib: blink maths, frame decoding limits, engine-missing handling."""

from __future__ import annotations

import base64

import pytest

from hcm_lite.services import face

OPEN_EYE = [(0, 0), (2, -1), (4, -1), (6, 0), (4, 1), (2, 1)]  # width 6, lids 2 apart -> EAR 0.333
CLOSED_EYE = [(0, 0), (2, -0.2), (4, -0.2), (6, 0), (4, 0.2), (2, 0.2)]  # EAR ~0.067


def test_eye_aspect_ratio():
    assert face.eye_aspect_ratio(OPEN_EYE) == pytest.approx(1 / 3, abs=1e-6)
    assert face.eye_aspect_ratio(CLOSED_EYE) == pytest.approx(0.0667, abs=1e-3)


@pytest.mark.parametrize("ears, expected", [
    ([0.33, 0.34, 0.12, 0.33], True),       # open, open, CLOSED, open -> blink
    ([0.33, 0.33, 0.32, 0.34, 0.33], False),  # a photo held up to the camera: no dip
    ([0.15, 0.14, 0.15], False),            # eyes never clearly open (too dark / squinting) -> not trusted
    ([0.30], False),                        # one frame can't show a blink
    ([0.28, 0.27, 0.21, 0.28], False),      # small wobble (75 % of open) is noise, not a blink
])
def test_blink_detected(ears, expected):
    assert face.blink_detected(ears) is expected


def test_decode_frame_rejects_garbage_and_oversize():
    with pytest.raises(face.ValidationFailed) as bad:
        face.decode_frame("data:image/jpeg;base64,!!!not base64!!!")
    assert bad.value.code == "bad_frame"
    huge = base64.b64encode(b"\xff" * (face.MAX_FRAME_BYTES + 1)).decode()
    with pytest.raises(face.ValidationFailed) as big:
        face.decode_frame(huge)
    assert big.value.code == "bad_frame"


def test_missing_engine_is_a_clear_503(api, company, monkeypatch):
    api.ok(api.put("admin", "/api/settings", {"attendance_mode": "camera"}))

    def unavailable():
        raise face.FaceEngineUnavailable("Face verification isn't installed on this server")

    monkeypatch.setattr(face, "get_engine", unavailable)
    frame = base64.b64encode(b"x" * 200).decode()
    resp = api.post("ali", "/api/attendance/face/enroll", {"frames": [frame] * 3})
    assert resp.status_code == 503 and resp.json()["error"] == "face_engine_unavailable"
