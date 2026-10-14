"""Authenticated batch management exports human labels and preserves sampling evidence."""

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from tvpet_console.annotations import AnnotationUpdate
from tvpet_console.app import create_app
from tvpet_console.config import Settings

ORIGIN = "https://testserver"
FIRST = "20261004-120000-100000"
SECOND = "20261004-120001-100000"
THIRD = "20261004-120002-100000"


def seed(root: Path, sample_id: str, people: int = 1) -> None:
    root.mkdir(exist_ok=True)
    image = io.BytesIO()
    Image.new("RGB", (640, 480), "gray").save(image, "JPEG")
    (root / f"{sample_id}.jpg").write_bytes(image.getvalue())
    (root / f"{sample_id}.json").write_text(
        json.dumps(
            {
                "sampled_at": "2026-10-04T12:00:00+08:00",
                "sequence": 1,
                "recognition": "active",
                "faces": [
                    {"bbox": [10 + i * 100, 20, 50 + i * 100, 80], "label": "爸爸"}
                    for i in range(people)
                ],
                "pose": {"state": "active", "people": []},
            }
        )
    )


def test_batch_api_exact_human_categories_zip_and_delete(tmp_path: Path) -> None:
    source = tmp_path / "samples"
    seed(source, FIRST, 2)
    seed(source, SECOND)
    seed(source, THIRD)
    app = create_app(Settings(tmp_path / "state", origin=ORIGIN, pose_review_dir=str(source)))
    app.state.store.initialize("synthetic-test-password")
    library = app.state.photos
    library.save(
        FIRST,
        AnnotationUpdate.model_validate(
            {
                "expected_revision": 0,
                "status": "confirmed",
                "people": [
                    {"id": "face-0", "identity": "爸爸", "posture": "standing", "action": "none"},
                    {"id": "face-1", "identity": "儿子", "posture": "lying", "action": "none"},
                ],
            }
        ),
    )
    library.save(
        SECOND,
        AnnotationUpdate.model_validate(
            {
                "expected_revision": 0,
                "status": "confirmed",
                "people": [
                    {"id": "face-0", "identity": "儿子", "posture": "sitting", "action": "none"}
                ],
            }
        ),
    )
    selection = {"ids": [], "identity": "爸爸", "posture": "", "label_source": "manual"}
    deletion = {"items": [{"id": FIRST, "expected_revision": 1}], "scope": "labels"}
    with TestClient(app, base_url=ORIGIN) as client:
        for endpoint, body in (
            ("/api/v1/photo-labels/export.zip", selection),
            ("/api/v1/photo-labels/delete", deletion),
        ):
            assert client.post(endpoint, json=body, headers={"Origin": ORIGIN}).status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "synthetic-test-password"},
            headers={"Origin": ORIGIN},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf"]}
        assert client.post("/api/v1/photo-labels/export.zip", json=selection).status_code == 403
        assert (
            client.post(
                "/api/v1/photo-labels/delete", json=deletion, headers={"Origin": ORIGIN}
            ).status_code
            == 403
        )
        listing = client.get("/api/v1/photos?identity=爸爸&label_source=manual").json()
        assert listing["total"] == 1
        assert (
            client.get("/api/v1/photos?identity=爸爸&label_source=effective").json()["total"] == 2
        )
        assert (
            client.get("/api/v1/photos?identity=爸爸&posture=lying&label_source=manual").json()[
                "total"
            ]
            == 0
        )
        assert (
            client.get("/api/v1/photos?identity=儿子&posture=sitting&label_source=manual").json()[
                "total"
            ]
            == 1
        )
        assert client.get("/api/v1/photos?label_source=invalid").status_code == 400
        archive = client.post("/api/v1/photo-labels/export.zip", json=selection, headers=headers)
        assert archive.status_code == 200 and archive.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            records = [json.loads(line) for line in bundle.read("dataset.jsonl").splitlines()]
            assert len(records) == 1 and records[0]["id"] == FIRST
            assert len(records[0]["annotation"]["people"]) == 2
            assert records[0]["matched_person_ids"] == ["face-0"]
            assert (
                bundle.read(f"samples/{FIRST}/annotated.jpg")
                == (source / f"{FIRST}.jpg").read_bytes()
            )
        assert not list((tmp_path / "state/photo-labels/exports").glob("*.zip"))
        wrong = {"items": [{"id": FIRST, "expected_revision": 0}], "scope": "labels"}
        assert (
            client.post("/api/v1/photo-labels/delete", json=wrong, headers=headers).status_code
            == 409
        )
        removed = client.post("/api/v1/photo-labels/delete", json=deletion, headers=headers)
        assert removed.status_code == 200 and removed.json()["deleted"] == 1
        item = client.get(f"/api/v1/photos/{FIRST}").json()
        assert item["annotation"] is None and item["revision"] > 1
        assert (source / f"{FIRST}.jpg").is_file()
        assert client.get(item["image_url"]).status_code == 200
        assert client.get("/api/v1/photos?identity=爸爸&label_source=manual").json()["total"] == 0
        stale = {
            "expected_revision": 0,
            "status": "confirmed",
            "people": [
                {"id": "face-0", "identity": "妈妈", "posture": "sitting", "action": "none"}
            ],
        }
        endpoint = f"/api/v1/photos/{FIRST}/annotation"
        assert client.post(endpoint, json=stale, headers=headers).status_code == 409
        stale["expected_revision"] = item["revision"]
        assert client.post(endpoint, json=stale, headers=headers).status_code == 200
        assert client.get("/api/v1/photos?identity=妈妈&label_source=manual").json()["total"] == 1
        current = client.get(f"/api/v1/photos/{FIRST}").json()
        photos = {
            "items": [
                {"id": FIRST, "expected_revision": current["revision"]},
                {"id": THIRD, "expected_revision": 0},
            ],
            "scope": "photos",
        }
        removed = client.post("/api/v1/photo-labels/delete", json=photos, headers=headers)
        assert removed.status_code == 200 and removed.json()["deleted"] == 2
        for sample_id in (FIRST, THIRD):
            assert client.get(f"/api/v1/photos/{sample_id}").status_code == 404
            assert client.get(f"/api/v1/photos/{sample_id}/image").status_code == 404
            assert not (source / f"{sample_id}.jpg").exists()
        assert client.get(f"/api/v1/photos/{SECOND}/image").status_code == 200
        assert client.get("/api/v1/photos").json()["total"] == 1
