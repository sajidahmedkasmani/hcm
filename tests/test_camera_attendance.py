"""Camera attendance (check-in AND check-out) — verified on the server with real face detection & matching.

Frames come from real photos (tests/fixtures/faces): person A is the employee, person B is an impostor.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.face_frames import blink_frames, frames_of, no_face, payload, two_faces

pytest.importorskip("dlib", reason="camera attendance needs dlib (pip install dlib-bin)")

CAMERA = "/api/attendance/camera"
ENROLL = "/api/attendance/face/enroll"


@pytest.fixture
def camera_company(api, company) -> dict[str, Any]:
    api.ok(api.put("admin", "/api/settings", {"attendance_mode": "camera"}))
    return company


def _enroll_ali(api) -> dict[str, Any]:
    return api.ok(api.post("ali", ENROLL, {"frames": frames_of("a", 3)}), 201)


def _code(resp) -> str:
    return resp.json()["error"]


# ------------------------------------------------------------------ the method itself
def test_new_company_defaults_to_camera_and_buttons_are_refused(api):
    api.ok(api.post(None, "/api/setup", {"company_name": "Fresh Co", "admin_email": "a@fresh.pk",
                                         "admin_password": "Admin@12345"}), 201)
    api.login("admin", "a@fresh.pk", "Admin@12345")
    assert api.ok(api.get("admin", "/api/settings"))["attendance_mode"] == "camera"


def test_one_click_buttons_are_refused_in_camera_mode(api, camera_company):
    for url in ("/api/attendance/check-in", "/api/attendance/check-out"):
        resp = api.post("ali", url)
        assert resp.status_code == 403 and _code(resp) == "camera_required"
    today = api.ok(api.get("ali", "/api/attendance/today"))
    assert today["attendance_mode"] == "camera" and today["face_enrolled"] is False and today["record"] is None


def test_camera_needs_enrollment_first(api, camera_company):
    resp = api.post("ali", CAMERA, payload("in", frames_of("a")))
    assert resp.status_code == 409 and _code(resp) == "face_not_enrolled"


# ------------------------------------------------------------------ enrollment
def test_self_enrollment_once_then_only_hr_can_change_it(api, camera_company):
    status = _enroll_ali(api)
    assert status["enrolled"] and status["method"] == "self" and status["samples"] == 3
    assert api.ok(api.get("ali", "/api/attendance/face"))["enrolled"] is True
    again = api.post("ali", ENROLL, {"frames": frames_of("a", 3)})
    assert again.status_code == 409 and _code(again) == "face_already_enrolled"
    log = api.ok(api.get("admin", "/api/audit", params={"entity": "face_profile"}))["items"]
    assert log[0]["action"] == "enroll" and "descriptor" not in str(log[0])  # biometric never written to the log


def test_enrollment_rejects_unsteady_or_bad_captures(api, camera_company):
    mixed = frames_of("a", 2) + frames_of("b", 1)
    resp = api.post("ali", ENROLL, {"frames": mixed})
    assert resp.status_code == 422 and _code(resp) == "face_not_steady"
    resp = api.post("ali", ENROLL, {"frames": two_faces()})
    assert resp.status_code == 422 and _code(resp) == "multiple_faces"
    resp = api.post("ali", ENROLL, {"frames": no_face()})
    assert resp.status_code == 422 and _code(resp) == "no_face"
    resp = api.post("ali", ENROLL, {"frames": ["not-a-picture" * 20] * 3})
    assert resp.status_code == 422 and _code(resp) == "bad_frame"
    resp = api.post("ali", ENROLL, {"frames": frames_of("a", 2)})  # too few
    assert resp.status_code == 422


def test_one_face_cannot_be_enrolled_on_two_accounts(api, camera_company):
    _enroll_ali(api)
    resp = api.post("sara", ENROLL, {"frames": frames_of("a", 3)})  # Ali tries to register his face on Sara's account
    assert resp.status_code == 409 and _code(resp) == "face_in_use"
    assert api.ok(api.get("sara", "/api/attendance/face"))["enrolled"] is False
    rejects = api.ok(api.get("admin", "/api/audit", params={"entity": "face_profile", "action": "reject"}))["items"]
    assert rejects and rejects[0]["after"]["reason"] == "face_in_use"


def test_hr_resets_and_enrolls_in_person_employees_cannot(api, camera_company):
    ali_id, sara_id = camera_company["people"]["ali"]["id"], camera_company["people"]["sara"]["id"]
    _enroll_ali(api)
    # an employee can't touch someone else's face profile
    assert api.post("sara", f"/api/employees/{ali_id}/face", {"frames": frames_of("b", 3)}).status_code == 403
    assert api.delete("sara", f"/api/employees/{ali_id}/face").status_code == 403
    # HR reset, then HR enrolls in person
    assert api.delete("hr", f"/api/employees/{ali_id}/face").status_code == 204
    assert api.ok(api.get("hr", f"/api/employees/{ali_id}/face"))["enrolled"] is False
    status = api.ok(api.post("hr", f"/api/employees/{ali_id}/face", {"frames": frames_of("a", 4)}), 201)
    assert status["method"] == "hr" and status["samples"] == 4
    # HR can also re-enroll directly without a reset (replaces the profile)
    api.ok(api.post("hr", f"/api/employees/{sara_id}/face", {"frames": frames_of("b", 3)}), 201)
    api.ok(api.post("hr", f"/api/employees/{sara_id}/face", {"frames": frames_of("b", 3)}), 201)
    # employees can't read another employee's status
    assert api.get("bilal", f"/api/employees/{ali_id}/face").status_code == 403


# ------------------------------------------------------------------ check-in / check-out
def test_camera_check_in_and_check_out(api, camera_company, clock):
    _enroll_ali(api)
    rec = api.ok(api.post("ali", CAMERA, payload("in", frames_of("a", 3))))
    assert rec["check_in"] == "08:30" and rec["source"] == "camera"
    clock.advance(hours=9, minutes=5)
    rec = api.ok(api.post("ali", CAMERA, payload("out", frames_of("a", 3))))
    assert rec["check_out"] == "17:35" and rec["status"] == "present"

    again = api.post("ali", CAMERA, payload("out", frames_of("a", 3)))
    assert again.status_code == 409 and _code(again) == "already_checked_out"
    board = api.ok(api.get("hr", "/api/attendance/board"))
    row = next(r for r in board["rows"] if r["full_name"] == "Ali Khan")
    assert row["source"] == "camera" and row["check_in"] == "08:30" and row["check_out"] == "17:35"


def test_check_out_without_check_in_is_refused(api, camera_company):
    _enroll_ali(api)
    resp = api.post("ali", CAMERA, payload("out", frames_of("a")))
    assert resp.status_code == 409 and _code(resp) == "not_checked_in"


def test_impostor_is_rejected_and_the_attempt_is_kept(api, camera_company):
    _enroll_ali(api)
    resp = api.post("ali", CAMERA, payload("in", frames_of("b", 3)))  # someone else at Ali's login
    assert resp.status_code == 422 and _code(resp) == "face_mismatch"
    assert resp.json()["details"]["distance"] > 0.5
    assert api.ok(api.get("ali", "/api/attendance/today"))["record"] is None
    rejects = api.ok(api.get("admin", "/api/audit", params={"entity": "camera_attendance"}))["items"]
    assert rejects[0]["action"] == "reject" and rejects[0]["after"]["reason"] == "face_mismatch"


def test_frame_problems_are_explained(api, camera_company):
    _enroll_ali(api)
    for frames, code in ((two_faces(), "multiple_faces"), (no_face(), "no_face")):
        resp = api.post("ali", CAMERA, payload("in", frames))
        assert resp.status_code == 422 and _code(resp) == code


@pytest.mark.parametrize("where, code", [
    ({"lat": 24.9000, "lng": 67.0011}, "outside_geofence"),  # ~4.3 km away
    ({"accuracy_m": 450.0}, "low_gps_accuracy"),
])
def test_geofence_is_decided_on_the_server(api, camera_company, where, code):
    _enroll_ali(api)
    resp = api.post("ali", CAMERA, payload("in", frames_of("a"), **where))
    assert resp.status_code == 422 and _code(resp) == code


def test_no_office_location_means_no_camera_attendance(api):
    api.ok(api.post(None, "/api/setup", {"company_name": "Fresh Co", "admin_email": "a@fresh.pk",
                                         "admin_password": "Admin@12345"}), 201)
    api.login("admin", "a@fresh.pk", "Admin@12345")
    dept = api.ok(api.post("admin", "/api/departments", {"name": "Ops"}), 201)
    hr = api.ok(api.post("admin", "/api/users/hr", {"full_name": "Zara", "email": "zara@fresh.pk",
                                                     "department_id": dept["id"], "join_date": "2026-01-01",
                                                     "basic_salary": 50000}), 201)
    api.onboard("zara", "zara@fresh.pk", hr["temporary_password"])
    api.ok(api.post("zara", ENROLL, {"frames": frames_of("a", 3)}), 201)
    resp = api.post("zara", CAMERA, payload("in", frames_of("a")))
    assert resp.status_code == 422 and _code(resp) == "no_office_location"


def test_location_and_face_proof_are_stored_without_images(api, camera_company, clock):
    _enroll_ali(api)
    api.ok(api.post("ali", CAMERA, payload("in", frames_of("a", 3), lat=24.8610, lng=67.0013)))
    clock.advance(hours=9)
    api.ok(api.post("ali", CAMERA, payload("out", frames_of("a", 3))))
    from hcm_lite.models import AttendanceRecord

    db = api.client.app.state.hcm.session_factory()
    try:
        record = db.query(AttendanceRecord).one()
        assert set(record.geo) == {"in", "out"}
        proof = record.geo["in"]
        assert proof["face_distance"] < 0.5 and proof["distance_m"] < 50 and proof["frames_with_face"] == 3
        assert "frames" in proof and not any(isinstance(v, str) and len(v) > 100 for v in proof.values())
    finally:
        db.close()


# ------------------------------------------------------------------ liveness (optional blink)
def test_blink_required_rejects_a_held_up_photo_and_accepts_a_blink(api, camera_company):
    api.ok(api.put("admin", "/api/settings", {"require_blink": True}))
    _enroll_ali(api)
    static = api.post("ali", CAMERA, payload("in", frames_of("a", 4)))  # a still photo never blinks
    assert static.status_code == 422 and _code(static) == "no_blink"
    rec = api.ok(api.post("ali", CAMERA, payload("in", blink_frames())))
    assert rec["source"] == "camera"


def test_switching_back_to_web_buttons(api, camera_company):
    api.ok(api.put("admin", "/api/settings", {"attendance_mode": "web"}))
    assert api.ok(api.post("ali", "/api/attendance/check-in"), 201)["source"] == "web"
