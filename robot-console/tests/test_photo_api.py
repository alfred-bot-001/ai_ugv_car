"""Photo review endpoints preserve session, CSRF and training data boundaries."""

import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from tvpet_console.app import create_app
from tvpet_console.config import Settings

ORIGIN = "https://testserver"
SAMPLE = "20261004-120000-123456"


def sample(root: Path) -> None:
    root.mkdir()
    image = io.BytesIO()
    Image.new("RGB", (640, 480), "#eeeeee").save(image, "JPEG")
    (root / f"{SAMPLE}.jpg").write_bytes(image.getvalue())
    (root / f"{SAMPLE}.json").write_text(
        json.dumps(
            {
                "sampled_at": "2026-10-04T12:00:00+08:00",
                "sequence": 123,
                "recognition": "active",
                "faces": [{"bbox": [10, 20, 50, 80], "label": "未登记", "similarity": 0.3}],
                "pose": {"state": "active", "people": []},
            }
        )
    )


def test_authenticated_photos_and_manual_revision(tmp_path: Path) -> None:
    source = tmp_path / "samples"
    sample(source)
    app = create_app(Settings(tmp_path / "state", origin=ORIGIN, pose_review_dir=str(source)))
    app.state.store.initialize("synthetic-test-password")
    with TestClient(app, base_url=ORIGIN) as client:
        for path in (
            "/api/v1/photos",
            f"/api/v1/photos/{SAMPLE}",
            f"/api/v1/photos/{SAMPLE}/image",
            "/api/v1/photo-labels/export",
        ):
            assert client.get(path).status_code == 401
        assert "欢迎回来" in client.get("/photos").text
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "synthetic-test-password"},
            headers={"Origin": ORIGIN},
        )
        assert login.status_code == 200
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf"]}
        assert "照片标注" in client.get("/photos").text
        listing = client.get("/api/v1/photos").json()
        assert listing["total"] == 1 and listing["stats"]["pending"] == 1
        item = client.get(f"/api/v1/photos/{SAMPLE}").json()
        assert not item["original_available"] and item["annotation"] is None
        picture = client.get(item["image_url"])
        assert picture.status_code == 200 and picture.headers["cache-control"] == "no-store"
        assert client.get(f"/api/v1/photos/{SAMPLE}/image?kind=original").status_code == 404
        assert client.get("/api/v1/photos?limit=101").status_code == 422
        assert client.get("/api/v1/photos?status=invalid").status_code == 400
        body = {
            "expected_revision": 0,
            "status": "confirmed",
            "people": [
                {"id": "face-0", "identity": "爸爸", "posture": "sitting", "action": "waving"}
            ],
            "notes": "人工核对，仅用于测试",
        }
        endpoint = f"/api/v1/photos/{SAMPLE}/annotation"
        assert client.post(endpoint, json=body).status_code == 403
        assert client.post(endpoint, json=body, headers={"Origin": ORIGIN}).status_code == 403
        assert client.post(endpoint, json=body, headers=headers).status_code == 200
        assert client.post(endpoint, json=body, headers=headers).status_code == 409
        reviewed = client.get(f"/api/v1/photos/{SAMPLE}").json()
        assert reviewed["annotation"]["revision"] == 1
        assert reviewed["annotation"]["people"][0]["identity"] == "爸爸"
        assert reviewed["predictions"][0]["identity"] == "未登记"
        export = client.get("/api/v1/photo-labels/export")
        assert export.status_code == 200 and len(export.text.strip().splitlines()) == 1
        assert "dataset.jsonl" in export.headers["content-disposition"]
        for path in source.glob("*.*"):
            path.unlink()
        assert client.get(item["image_url"]).status_code == 200
        body.update(expected_revision=1, status="excluded")
        assert client.post(endpoint, json=body, headers=headers).status_code == 200
        assert not client.get("/api/v1/photo-labels/export").text.strip()
        assert client.get("/api/v1/photos?status=excluded").json()["total"] == 1
        assert client.get("/api/v1/photos/not-a-sample").status_code == 404
        assert client.get("/static/../state/photo-labels/dataset.jsonl").status_code == 404


def test_photo_storage_disabled_is_empty(tmp_path: Path) -> None:
    app = create_app(Settings(tmp_path, origin=ORIGIN))
    app.state.store.initialize("synthetic-test-password")
    with TestClient(app, base_url=ORIGIN) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "synthetic-test-password"},
            headers={"Origin": ORIGIN},
        )
        assert client.get("/api/v1/photos").json()["items"] == []
