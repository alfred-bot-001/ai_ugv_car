"""Verify synthetic face overlays, responsive layout and failure states in Chromium."""

import argparse
import asyncio
import json
import mimetypes
import struct
import zlib
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1] / "robot-console/src/tvpet_console/web"
parser = argparse.ArgumentParser(description="验证模拟人脸视频的浏览器显示")
parser.add_argument("--output", type=Path, default=Path("/tmp/tvpet-face-ui"))
OUT = parser.parse_args().output


def chunk(kind, data):
    return (
        struct.pack("!I", len(data))
        + kind
        + data
        + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


png = (
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack("!2I5B", 640, 480, 8, 2, 0, 0, 0))
    + chunk(
        b"IDAT", zlib.compress(b"".join(b"\x00" + bytes([37, 65, 54]) * 640 for y in range(480)))
    )
    + chunk(b"IEND", b"")
)
metadata = {
    "sequence": 42,
    "recognition": "active",
    "inference_ms": 78,
    "faces": [
        {"bbox": [70, 110, 190, 270], "label": "爸爸", "known": True},
        {"bbox": [270, 170, 365, 310], "label": "儿子", "known": True},
        {"bbox": [420, 105, 540, 265], "label": "妈妈", "known": True},
    ],
}
metadata["pose"] = {
    "state": "active",
    "people": [
        {
            "track_id": 1,
            "identity": "爸爸",
            "posture_label": "站立",
            "action_label": "举手",
            "keypoints": [[0.5, 0.5, 0, 0.99] for _ in range(33)],
        }
    ],
}
state = {
    "online": True,
    "asr_ready": True,
    "state": {
        "system": {"sampled_at": 0},
        "board": {"state": "unknown", "gate_reason": "测试画面"},
        "camera": {"state": "available"},
        "edge": {"state": "disabled"},
    },
}


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for size, name in [
            ({"width": 1440, "height": 1000}, "desktop"),
            ({"width": 390, "height": 844}, "mobile"),
        ]:
            context = await browser.new_context(viewport=size)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))

            async def route(request):
                path = request.request.url.split("console.test", 1)[1].split("?")[0]
                headers = {
                    "Content-Security-Policy": (
                        "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
                        "script-src 'self'; connect-src 'self'; media-src 'self' blob:; "
                        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                    )
                }
                if path == "/":
                    await request.fulfill(
                        body=Environment(loader=FileSystemLoader(ROOT))
                        .get_template("dashboard.html")
                        .render(),
                        content_type="text/html",
                        headers=headers,
                    )
                elif path == "/api/v1/auth/session":
                    await request.fulfill(json={"csrf": "synthetic-test-only"})
                elif path == "/api/v1/status":
                    await request.fulfill(json=state)
                elif path.startswith("/static/"):
                    file = ROOT / path.removeprefix("/static/")
                    await request.fulfill(
                        body=file.read_bytes(),
                        content_type=mimetypes.guess_type(str(file))[0]
                        or "application/octet-stream",
                    )
                else:
                    await request.fulfill(status=404)

            await page.route("http://console.test/**", route)
            video = []

            def wsroute(ws, video=video):
                if ws.url.endswith("/video"):
                    video.append(ws)

            await page.route_web_socket("**/ws/**", wsroute)
            await page.goto("http://console.test/")
            await page.locator("#online-badge").filter(has_text="老三在线").wait_for()
            await page.click("#video-toggle")
            for _ in range(30):
                if video:
                    break
                await asyncio.sleep(0.02)
            assert video

            def send(meta, video=video):
                encoded = json.dumps(meta, ensure_ascii=False).encode()
                video[0].send(struct.pack("!I", len(encoded)) + encoded + png)

            send(metadata)
            await page.locator("#face-detail").filter(has_text="爸爸 · 儿子 · 妈妈").wait_for()
            canvas = page.locator("#video-frame")
            dims = await canvas.evaluate("(e)=>[e.width,e.height]")
            assert dims == [640, 480]
            pixel = await canvas.evaluate(
                '(e)=>Array.from(e.getContext("2d").getImageData(70,150,1,1).data)'
            )
            assert pixel[:3] == [197, 245, 138], pixel
            joint = await canvas.evaluate(
                '(e)=>Array.from(e.getContext("2d").getImageData(320,240,1,1).data)'
            )
            assert joint[:3] == [255, 212, 124], joint
            assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            OUT.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(OUT / ("face-overlay-" + name + ".png")), full_page=True)
            fallback = dict(metadata, faces=[], recognition="unavailable", pose={"people": []})
            send(fallback)
            await page.locator("#face-state").filter(has_text="识别服务暂不可用").wait_for()
            pixel = await canvas.evaluate(
                '(e)=>Array.from(e.getContext("2d").getImageData(70,150,1,1).data)'
            )
            assert pixel[:3] == [37, 65, 54], pixel
            await asyncio.sleep(2.1)
            assert await canvas.is_hidden()
            send(metadata)
            await page.locator("#face-detail").filter(has_text="爸爸 · 儿子 · 妈妈").wait_for()
            await page.click("#video-toggle")
            assert await canvas.is_hidden()
            assert not errors, errors
            print(
                name,
                "Chinese labels, coordinates, layout, fallback and stream recovery passed",
            )
            await context.close()
        await browser.close()


asyncio.run(main())
