"""Authenticated web core and independent outbound device transport endpoints."""

import asyncio
import base64
import binascii
import contextlib
import json
import re
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint
from tvpet_pi.nav_ops import NavCommand
from tvpet_pi.protocol import (
    AGENT_PROTOCOL,
    Credentials,
    Lights,
    PasswordChange,
    SpeechRequest,
    Stick,
)

from .annotations import (
    AnnotationConflict,
    AnnotationLibrary,
    AnnotationUpdate,
)
from .config import Settings
from .downloads import ExportDownloads, TemporaryFileResponse
from .gateway import DeviceError, Gateway
from .middleware import LimitBody
from .photo_batches import BatchDelete, BatchSelection
from .pose_review import ReviewRecorder
from .speech import SpeechService, synthesize
from .sso import TRANSACTION_COOKIE, TRANSACTION_TTL, SSOClient, SSOConfig, SSOError
from .store import Identity, Store, csrf_proof, digest

COOKIE = "tvpet_session"
WEB = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    sso_config = SSOConfig.load(settings.sso_config_file)
    if sso_config and (settings.origin != "https://car.aimaker.vip" or not settings.secure_cookie):
        raise ValueError("SSO 必须使用 https://car.aimaker.vip 和安全 Cookie")
    sso = SSOClient(sso_config) if sso_config else None
    store, gateway = (
        Store(settings.data_dir),
        Gateway(settings.face_service_url, bool(settings.pose_review_dir)),
    )
    speech = SpeechService(settings.asr_model)
    photos = AnnotationLibrary(settings.pose_review_dir, settings.data_dir)
    downloads = ExportDownloads()
    gateway.faces.recorder = ReviewRecorder(
        settings.pose_review_dir, settings.pose_review_font, state_dir=settings.data_dir
    )
    gateway.background_policy = gateway.faces.recorder.can_capture
    gateway.background_video = False
    attempts: OrderedDict[str, tuple[float, int]] = OrderedDict()
    browsers: set[WebSocket] = set()
    browser_tokens: dict[WebSocket, str] = {}
    control_owner = ""
    templates = Environment(loader=FileSystemLoader(WEB), autoescape=select_autoescape())

    async def revoke() -> None:
        nonlocal control_owner
        control_owner = ""
        # Close browser access before waiting for device acknowledgements.
        for socket in list(browsers):
            with contextlib.suppress(Exception):
                await socket.close(code=4001)
        await gateway.revoke()

    async def authenticate(
        token: str, *, touch: bool = False, cached: bool = False
    ) -> Identity | None:
        current = store.authenticate(token)
        if current is None:
            return None
        if sso:
            grant = await sso.valid(current.token_hash, cached=cached)
            if grant is None:
                store.logout(current)
                await revoke()
                return None
            current = replace(current, username=grant.username, auth_mode="sso")
        if touch:
            if store.authenticate(token, touch=True) is None:
                return None
        return current

    async def monitor_browser_sessions() -> None:
        while True:
            await asyncio.sleep(1)
            if sso:
                for token in set(browser_tokens.values()):
                    if await authenticate(token, cached=True) is None:
                        await revoke()
                        break

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        face_worker = asyncio.create_task(gateway.faces.run())
        review_worker = asyncio.create_task(gateway.faces.recorder.run())
        background_worker = asyncio.create_task(gateway.background_capture())
        auth_worker = asyncio.create_task(monitor_browser_sessions())
        try:
            yield
        finally:
            await revoke()
            for task in (face_worker, review_worker, background_worker, auth_worker):
                task.cancel()
            await asyncio.gather(
                face_worker, review_worker, background_worker, auth_worker, return_exceptions=True
            )
            if gateway.control:
                with contextlib.suppress(Exception):
                    await gateway.control.close(code=1012)
            if sso:
                await sso.close()
            await downloads.close()
            store.engine.dispose()

    app = FastAPI(title="老三主控台", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.gateway, app.state.speech = store, gateway, speech
    app.state.photos = photos
    app.state.sso = sso
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/api/") and request.method == "POST":
            if request.headers.get("origin") != settings.origin:
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
            if len(await request.body()) > 16384:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'"
        )
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    async def identity(request: Request, *, mutate: bool = False) -> Identity:
        current = await authenticate(request.cookies.get(COOKIE, ""), touch=mutate)
        if not current:
            raise HTTPException(401, "请登录主控台")
        if mutate and not store.csrf_valid(current, request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "会话校验失败，请重新登录")
        return current

    async def rpc(
        action: str, args: dict[str, Any] | None = None, *, timeout: float = 2, event: str = ""
    ) -> dict[str, Any]:
        try:
            result = await gateway.rpc(action, args, timeout=timeout)
            store.audit(event or f"device.{action}")
            return result
        except (ValueError, TimeoutError) as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "admin_initialized": store.initialized(),
            "agent_online": gateway.online,
            "service": "robot-console",
            "mode": gateway.state.get("mode", "observe") if gateway.online else "observe",
        }

    async def render_page(request: Request, template: str, next_path: str) -> Response:
        current = await authenticate(request.cookies.get(COOKIE, ""))
        if sso and current is None:
            return RedirectResponse("/auth/sso/login?next=" + next_path, status_code=303)
        return HTMLResponse(
            templates.get_template(template if current else "login.html").render(
                initialized=store.initialized(),
                sso=bool(sso),
                username=current.username if current else "admin",
            )
        )

    @app.get("/", response_class=HTMLResponse)
    async def page(request: Request) -> Response:
        return await render_page(request, "dashboard.html", "/")

    @app.get("/navigation", response_class=HTMLResponse)
    async def navigation_page(request: Request) -> Response:
        return await render_page(request, "navigation.html", "/navigation")

    @app.get("/photos", response_class=HTMLResponse)
    async def photo_page(request: Request) -> Response:
        return await render_page(request, "photos.html", "/photos")

    @app.get("/auth/sso/login")
    async def sso_login(
        next_path: str = Query(default="/", alias="next", max_length=32),
    ) -> Response:
        if sso is None:
            raise HTTPException(404, "未开启统一登录")
        try:
            transaction, url = sso.begin(next_path)
        except SSOError as error:
            raise HTTPException(400, str(error)) from error
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            TRANSACTION_COOKIE,
            transaction,
            max_age=TRANSACTION_TTL,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/auth/sso",
        )
        return response

    @app.get("/auth/sso/callback")
    async def sso_callback(
        request: Request,
        code: str = Query(default="", max_length=1024),
        state: str = Query(default="", max_length=128),
    ) -> Response:
        if sso is None:
            raise HTTPException(404, "未开启统一登录")
        try:
            grant, next_path = await sso.exchange(
                request.cookies.get(TRANSACTION_COOKIE, ""), state, code
            )
            token, _ = store.login_sso()
            try:
                sso.link(digest(token), grant)
            except SSOError:
                current = store.authenticate(token)
                if current:
                    store.logout(current)
                raise
            old = store.authenticate(request.cookies.get(COOKIE, ""))
            if old:
                store.logout(old)
                sso.grants.pop(old.token_hash, None)
                await revoke()
            response: Response = RedirectResponse(next_path, status_code=303)
            response.set_cookie(
                COOKIE, token, max_age=43200, httponly=True, secure=True, samesite="strict"
            )
        except (SSOError, ValueError) as error:
            status = 403 if str(error) == "设备控制台仅允许家庭管理员访问" else 400
            response = JSONResponse({"detail": str(error)}, status_code=status)
        response.delete_cookie(
            TRANSACTION_COOKIE, path="/auth/sso", secure=True, httponly=True, samesite="strict"
        )
        return response

    @app.get("/api/v1/photos")
    async def photo_list(
        request: Request,
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=24, ge=1, le=100),
        status: str = Query(default="all", max_length=16),
        q: str = Query(default="", max_length=64),
        identity_filter: str = Query(default="", alias="identity", max_length=64),
        posture: str = Query(default="", max_length=16),
        label_source: str = Query(default="effective", max_length=16),
    ) -> dict[str, Any]:
        await identity(request)
        try:
            return await asyncio.to_thread(
                photos.list_samples, page, limit, status, q, identity_filter, posture, label_source
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/v1/photo-labels/export")
    async def photo_export(request: Request) -> Response:
        await identity(request)
        try:
            payload = await asyncio.to_thread(photos.manifest)
        except (OSError, ValueError) as error:
            raise HTTPException(507, "训练清单读取失败，请检查本机存储") from error
        return Response(
            payload,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="dataset.jsonl"'},
        )

    @app.post("/api/v1/photo-labels/export.zip")
    async def photo_export_zip(request: Request, body: BatchSelection) -> Response:
        await identity(request, mutate=True)
        try:
            path = await downloads.build(lambda: photos.export_zip(body))
        except AnnotationConflict as error:
            raise HTTPException(409, "标注已更新，请重新载入后导出") from error
        except FileNotFoundError as error:
            raise HTTPException(404, "所选照片已不存在，请重新载入") from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except OSError as error:
            raise HTTPException(507, "导出失败，请检查本机空间") from error
        return TemporaryFileResponse(
            path,
            media_type="application/zip",
            filename="photo-labels.zip",
        )

    @app.post("/api/v1/photo-labels/delete")
    async def photo_delete_labels(request: Request, body: BatchDelete) -> dict[str, Any]:
        await identity(request, mutate=True)
        try:
            result = await asyncio.to_thread(photos.delete_labels, body)
            store.audit(
                "photo.samples.bulk_deleted"
                if result.get("scope") == "photos"
                else "photo.labels.bulk_deleted"
            )
            return result
        except AnnotationConflict as error:
            raise HTTPException(409, "所选标注已更新，请重新同步选择后删除") from error
        except FileNotFoundError as error:
            raise HTTPException(404, "所选标注已不存在，请重新载入") from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except OSError as error:
            raise HTTPException(507, "删除未完成，请重新载入检查并核对本机存储") from error

    @app.get("/api/v1/photos/{sample_id}")
    async def photo_detail(request: Request, sample_id: str) -> dict[str, Any]:
        await identity(request)
        try:
            return await asyncio.to_thread(photos.detail, sample_id)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(404, "照片不存在") from error

    @app.get("/api/v1/photos/{sample_id}/image")
    async def photo_image(
        request: Request, sample_id: str, kind: str = Query(default="annotated", max_length=16)
    ) -> Response:
        await identity(request)
        try:
            payload = await asyncio.to_thread(photos.image, sample_id, kind)
            return Response(payload, media_type="image/jpeg")
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(404, "照片不存在或未保留原片") from error

    @app.post("/api/v1/photos/{sample_id}/annotation")
    async def annotate_photo(
        request: Request, sample_id: str, body: AnnotationUpdate
    ) -> dict[str, Any]:
        await identity(request, mutate=True)
        try:
            result = await asyncio.to_thread(photos.save, sample_id, body)
            store.audit("photo.annotation.saved")
            return result
        except AnnotationConflict as error:
            raise HTTPException(409, "照片已在另一页面修改，请重新载入后标注") from error
        except FileNotFoundError as error:
            raise HTTPException(404, "照片不存在") from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except OSError as error:
            raise HTTPException(507, "保存未完成，请检查本机空间并重新载入确认当前修订") from error

    @app.post("/api/v1/auth/login")
    async def login(request: Request, body: Credentials) -> Response:
        if sso:
            raise HTTPException(403, "请通过家庭控制台统一登录")
        if not store.initialized():
            raise HTTPException(503, "请在 Mini 终端初始化 admin 密码")
        address = request.client.host if request.client else "unknown"
        now = time.monotonic()
        start, count = attempts.get(address, (now, 0))
        if now - start > 600:
            start, count = now, 0
        attempts[address] = (start, count + 1)
        if len(attempts) > 1000:
            attempts.popitem(last=False)
        if count >= 8:
            raise HTTPException(429, "尝试过于频繁，请 10 分钟后再试")
        result = await asyncio.to_thread(store.login, body.password)
        if not result:
            raise HTTPException(401, "用户名或密码错误")
        attempts.pop(address, None)
        token, csrf = result
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            COOKIE,
            token,
            max_age=43200,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
        )
        return response

    @app.get("/api/v1/auth/session")
    async def session(request: Request) -> dict[str, str]:
        current = await identity(request)
        return {
            "username": current.username,
            "auth_mode": current.auth_mode,
            "csrf": csrf_proof(request.cookies.get(COOKIE, "")),
        }

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request) -> Response:
        # Self-logout must remain possible when the issuer is unreachable.
        current = store.authenticate(request.cookies.get(COOKIE, ""))
        if current is None:
            raise HTTPException(401, "请登录主控台")
        if not store.csrf_valid(current, request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "会话校验失败，请重新登录")
        # Capture before any await: an in-flight auth check can invalidate the mapping.
        grant = sso.grants.pop(current.token_hash, None) if sso else None
        store.logout(current)
        await revoke()
        upstream_ok = await sso.logout(grant) if sso else True
        response = (
            JSONResponse({"ok": True})
            if upstream_ok
            else JSONResponse(
                {"detail": "设备控制台已退出，家庭控制台暂不可用，请前往家庭控制台完成退出"},
                status_code=503,
            )
        )
        response.delete_cookie(
            COOKIE, secure=settings.secure_cookie, httponly=True, samesite="strict"
        )
        return response

    @app.post("/api/v1/auth/password")
    async def password(request: Request, body: PasswordChange) -> Response:
        if sso:
            raise HTTPException(403, "请在家庭控制台修改密码")
        await identity(request, mutate=True)
        changed = await asyncio.to_thread(
            store.change_password, body.old_password, body.new_password
        )
        if not changed:
            raise HTTPException(403, "旧密码不正确")
        await revoke()
        response = JSONResponse({"ok": True, "relogin": True})
        response.delete_cookie(
            COOKIE, secure=settings.secure_cookie, httponly=True, samesite="strict"
        )
        return response

    @app.get("/api/v1/status")
    async def status(request: Request) -> dict[str, Any]:
        await identity(request)
        return {
            **gateway.status(),
            "asr_ready": speech.asr_ready,
            "tts_ready": sys.platform == "darwin",
            "face_recognition": gateway.faces.status(),
            "pose_review": gateway.faces.recorder.status(),
        }

    @app.post("/api/v1/snapshot")
    async def snapshot(request: Request) -> Response:
        await identity(request, mutate=True)
        try:
            jpeg = await gateway.snapshot()
            store.audit("device.snapshot")
            return Response(jpeg, media_type="image/jpeg")
        except (ValueError, TimeoutError) as error:
            raise HTTPException(409, "拍照失败：" + str(error)) from error

    @app.post("/api/v1/control/lease")
    async def lease(request: Request) -> dict[str, Any]:
        nonlocal control_owner
        current = await identity(request, mutate=True)
        if control_owner and control_owner != current.token_hash:
            raise HTTPException(409, "另一会话正在控制")
        result = await rpc("lease")
        control_owner = current.token_hash
        return result

    @app.post("/api/v1/control/lights")
    async def lights(request: Request, body: Lights) -> dict[str, Any]:
        """Switch the chassis headlights or the gimbal light; the control owner is untouched."""
        await identity(request, mutate=True)
        result = await rpc("lights", body.model_dump(exclude_none=True))
        return {"lights": result.get("lights", {})}

    @app.post("/api/v1/control/{action}")
    async def control(request: Request, action: str) -> dict[str, Any]:
        nonlocal control_owner
        await identity(request, mutate=True)
        if action not in ("stop", "estop", "reset"):
            raise HTTPException(404, "未知控制")
        result = await rpc(action)
        control_owner = ""
        return result

    @app.post("/api/v1/nav/command")
    async def nav_command(request: Request, body: NavCommand) -> dict[str, Any]:
        """Map page controls; the car's ugv-navd runs them and holds any motion window."""
        await identity(request, mutate=True)
        if body.op in ("status", "maps", "preview", "route", "reports"):
            raise HTTPException(404, "未知导航操作")
        result = await rpc("nav", body.payload(), timeout=4, event=f"nav.{body.op}")
        return dict(result.get("nav") or {})

    @app.get("/api/v1/nav/maps")
    async def nav_maps(request: Request) -> dict[str, Any]:
        await identity(request)
        try:
            result = await gateway.rpc("nav", {"op": "maps"}, timeout=4)
        except (ValueError, TimeoutError) as error:
            raise HTTPException(409, str(error) or "读取地图列表超时") from error
        nav = result.get("nav") or {}
        maps, default = nav.get("maps"), nav.get("default")
        return {
            "maps": maps if isinstance(maps, list) else [],
            "default": default if isinstance(default, str) else None,
        }

    async def nav_read(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await gateway.rpc("nav", args, timeout=4)
        except (ValueError, TimeoutError) as error:
            raise HTTPException(409, str(error) or "读取超时") from error
        return dict(result.get("nav") or {})

    @app.get("/api/v1/nav/routes/{name}")
    async def nav_route(request: Request, name: str) -> dict[str, Any]:
        await identity(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise HTTPException(404, "没有这张地图")
        route = (await nav_read({"op": "route", "map": name})).get("route")
        return {"route": route if isinstance(route, dict) else None}

    @app.get("/api/v1/nav/reports")
    async def nav_reports(request: Request) -> dict[str, Any]:
        await identity(request)
        reports = (await nav_read({"op": "reports"})).get("reports")
        return {"reports": reports if isinstance(reports, list) else []}

    @app.get("/api/v1/nav/maps/{name}/preview.png")
    async def nav_map_preview(request: Request, name: str) -> Response:
        """A saved map's thumbnail (the home's floor plan): logged-in only, never cached."""
        await identity(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise HTTPException(404, "没有这张地图")
        try:
            result = await gateway.rpc("nav", {"op": "preview", "map": name}, timeout=6)
            png = base64.b64decode(str((result.get("nav") or {}).get("png", "")), validate=True)
        except (ValueError, TimeoutError, binascii.Error) as error:
            raise HTTPException(404, str(error) or "读取缩略图失败") from error
        if not png.startswith(b"\x89PNG") or len(png) > 1_000_000:
            raise HTTPException(502, "缩略图格式无效")
        return Response(png, media_type="image/png")  # the middleware adds no-store

    @app.post("/api/v1/speech/say")
    async def say(request: Request, body: SpeechRequest) -> dict[str, Any]:
        current = await identity(request, mutate=True)
        if not gateway.online or speech.tts_lock.locked():
            raise HTTPException(409, "老三离线或语音任务正在进行")
        async with speech.tts_lock:
            try:
                wav = await asyncio.to_thread(synthesize, body.text, body.volume)
                if not await authenticate(request.cookies.get(COOKIE, "")):
                    raise HTTPException(401, "会话已撤销")
                if current.generation != (await identity(request)).generation:
                    raise HTTPException(401, "会话已撤销")
                await gateway.play_audio(wav)
                store.audit("speech.playback_submitted")
                return {"ok": True, "state": "submitted"}
            except (ValueError, OSError, TimeoutError) as error:
                raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/speech/transcribe")
    async def transcribe(request: Request) -> dict[str, str]:
        await identity(request, mutate=True)
        if not speech.asr_ready:
            raise HTTPException(409, "尚未安装 Mini 本地转写模型")
        if speech.lock.locked() or not gateway.online:
            raise HTTPException(409, "设备离线或语音任务忙")
        async with speech.lock:
            pcm = bytearray()
            await gateway.viewer("audio", 1)
            try:
                last = gateway.audio_version
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    if not await authenticate(request.cookies.get(COOKIE, "")):
                        raise HTTPException(401, "会话已撤销")
                    async with gateway.audio_event:
                        await asyncio.wait_for(gateway.audio_event.wait(), timeout=2)
                        if gateway.audio_version != last:
                            pcm.extend(gateway.audio)
                            last = gateway.audio_version
                text = await asyncio.to_thread(speech.transcribe, bytes(pcm))
                await identity(request)
                return {"text": text}
            except (ValueError, TimeoutError) as error:
                raise HTTPException(409, "转写失败：" + str(error)) from error
            finally:
                await gateway.viewer("audio", -1)
                pcm.clear()

    async def browser_auth(
        websocket: WebSocket, *, video: bool = False
    ) -> tuple[Identity, str] | None:
        token = websocket.cookies.get(COOKIE, "")
        current = await authenticate(token)
        if websocket.headers.get("origin") != settings.origin or current is None:
            await websocket.close(code=1008)
            return None
        protocols = websocket.headers.get("sec-websocket-protocol", "").split(",")
        protocol = (
            "console.video.v2"
            if video and "console.video.v2" in [item.strip() for item in protocols]
            else "console.v1"
        )
        await websocket.accept(subprotocol=protocol)
        try:
            proof = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            if (
                not isinstance(proof, dict)
                or not store.csrf_valid(current, str(proof.get("csrf", "")))
                or await authenticate(token) is None
            ):
                await websocket.close(code=1008)
                return None
        except (TimeoutError, WebSocketDisconnect, ValueError):
            await websocket.close(code=1008)
            return None
        browsers.add(websocket)
        browser_tokens[websocket] = token
        return current, token

    @app.websocket("/ws/control")
    async def control_socket(websocket: WebSocket) -> None:
        """Relay the browser's held stick states to the car as they arrive.

        No per-command challenge: the car keeps driving the last state it was given
        for 0.5 s, so the link only has to deliver a state about ten times a second.
        A refused state is reported with its code; the browser leases again or stops.
        """
        nonlocal control_owner
        auth = await browser_auth(websocket)
        if auth is None:
            return
        current, token = auth
        latest: dict[str, Stick] = {}
        wake = asyncio.Event()

        async def relay() -> None:
            while True:
                await wake.wait()
                wake.clear()
                while latest:
                    stick = latest.pop(next(iter(latest)))
                    if control_owner != current.token_hash:
                        await websocket.send_json(
                            {"error": "请先按住圆盘取得控制", "code": "lease"}
                        )
                        continue
                    try:
                        await gateway.rpc("stick", stick.model_dump(), timeout=1)
                    except TimeoutError:
                        continue  # superseded by the next state; the car stops by itself
                    except DeviceError as error:
                        await websocket.send_json({"error": str(error), "code": error.code})
                        continue
                    except ValueError as error:
                        await websocket.send_json({"error": str(error), "online": False})
                        continue
                    await websocket.send_json({"ok": True, "sequence": stick.sequence})

        async def pulse() -> None:
            while await authenticate(token, cached=True):
                await websocket.send_json({"online": gateway.online})
                await asyncio.sleep(1)
            await websocket.close(code=4401)

        tasks = [asyncio.create_task(relay()), asyncio.create_task(pulse())]
        try:
            while True:
                raw = await websocket.receive_json()
                stick = Stick.model_validate(raw)
                latest[stick.target] = stick
                wake.set()
        except (ValueError, ValidationError) as error:
            with contextlib.suppress(Exception):
                await websocket.send_json({"error": str(error) or "控制报文无效"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            browsers.discard(websocket)
            browser_tokens.pop(websocket, None)
            if control_owner == current.token_hash:
                control_owner = ""
                if gateway.online:
                    with contextlib.suppress(ValueError, TimeoutError):
                        await gateway.rpc("stop")
            with contextlib.suppress(Exception):
                await websocket.close()

    @app.websocket("/ws/nav")
    async def nav_socket(websocket: WebSocket) -> None:
        """Live map page feed: a snapshot first, then every new packet (read-only)."""
        auth = await browser_auth(websocket)
        if auth is None:
            return
        _, token = auth
        acquired = False
        disconnected = asyncio.create_task(websocket.receive())
        try:
            await gateway.viewer("nav", 1)
            acquired = True
            seq, packets = gateway.nav.snapshot()
            while await authenticate(token, cached=True) and gateway.online:
                for data in packets:
                    await asyncio.wait_for(websocket.send_bytes(data), timeout=0.5)
                if disconnected.done():
                    break
                async with gateway.nav_event:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(gateway.nav_event.wait(), timeout=1)
                seq, update = gateway.nav.since(seq)
                if update is None:
                    seq, update = gateway.nav.snapshot()
                packets = update
        except (WebSocketDisconnect, ValueError, TimeoutError, RuntimeError):
            pass
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            browsers.discard(websocket)
            browser_tokens.pop(websocket, None)
            if acquired:
                with contextlib.suppress(ValueError, TimeoutError):
                    await gateway.viewer("nav", -1)
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

    @app.websocket("/ws/{channel}")
    async def media_socket(websocket: WebSocket, channel: str) -> None:
        if channel not in ("video", "audio", "front"):
            await websocket.close(code=1008)
            return
        auth = await browser_auth(websocket, video=channel == "video")
        if auth is None:
            return
        _, token = auth
        acquired = False
        disconnected = asyncio.create_task(websocket.receive())
        try:
            await gateway.viewer(channel, 1)
            acquired = True
            annotated = channel == "video" and "console.video.v2" in [
                item.strip()
                for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
            ]
            condition = (
                gateway.faces.output_event
                if annotated
                else gateway.video_event
                if channel == "video"
                else gateway.front_event
                if channel == "front"
                else gateway.audio_event
            )
            last = -1
            while await authenticate(token, cached=True) and gateway.online:
                if disconnected.done():
                    break
                async with condition:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(condition.wait(), timeout=1)
                    version = (
                        gateway.faces.version
                        if annotated
                        else gateway.video_version
                        if channel == "video"
                        else gateway.front_version
                        if channel == "front"
                        else gateway.audio_version
                    )
                    data = (
                        gateway.faces.packet
                        if annotated
                        else gateway.video
                        if channel == "video"
                        else gateway.front
                        if channel == "front"
                        else gateway.audio
                    )
                    if version == last or not data:
                        continue
                    last = version
                if await authenticate(token, cached=True) is None:
                    break
                await asyncio.wait_for(websocket.send_bytes(data), timeout=0.5)
        except (WebSocketDisconnect, ValueError, TimeoutError, RuntimeError):
            pass
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            browsers.discard(websocket)
            browser_tokens.pop(websocket, None)
            if acquired:
                with contextlib.suppress(ValueError, TimeoutError):
                    await gateway.viewer(channel, -1)
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

    @app.websocket("/internal/v1/agents/{agent_id}/{channel}")
    async def agent_socket(websocket: WebSocket, agent_id: str, channel: str) -> None:
        token = websocket.headers.get("authorization", "").removeprefix("Bearer ")
        allowed = (
            channel in ("control", "video", "audio", "nav", "front")
            and agent_id == settings.agent_id
            and store.agent_valid(agent_id, token)
            and AGENT_PROTOCOL in websocket.headers.get("sec-websocket-protocol", "")
        )
        if channel == "control":
            allowed = allowed and gateway.control is None
        else:
            allowed = (
                allowed
                and bool(gateway.link_id)
                and websocket.headers.get("x-agent-link") == gateway.link_id
            )
        if not allowed:
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=AGENT_PROTOCOL)
        try:
            if channel == "control":
                await gateway.control_loop(websocket)
            elif channel == "nav":
                await gateway.nav_loop(websocket)
            else:
                await gateway.media_loop(websocket, channel)
        except (WebSocketDisconnect, ValueError, TimeoutError, RuntimeError, json.JSONDecodeError):
            with contextlib.suppress(Exception):
                await websocket.close(code=1008)

    app.add_middleware(LimitBody)
    return app
