"""Local administrator bootstrap without argv, default or retained passwords."""

import argparse
import getpass
import os
import secrets
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Settings
from .store import Store


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="老三主控台 · Mini 核心")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("admin")
    admin.add_argument("action", choices=["init"])
    agent = commands.add_parser("agent")
    agent.add_argument("action", choices=["register"])
    agent.add_argument("--id", default="laosan")
    agent.add_argument("--token-file", type=Path, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8788)
    serve.add_argument("--cert")
    serve.add_argument("--key")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "admin":
        password = getpass.getpass("设置 admin 密码（至少 12 位）：")
        if password != getpass.getpass("再次输入："):
            parser.error("两次输入不一致")
        Store(settings.data_dir).initialize(password)
        print("admin 已初始化，密码仅保存 Argon2id 哈希。")
    elif args.command == "agent":
        if args.token_file.exists():
            parser.error("token 文件已存在")
        token = secrets.token_urlsafe(32)
        Store(settings.data_dir).register_agent(args.id, token)
        args.token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(args.token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write(token)
        print("设备已注册，凭据写入指定的 0600 文件。")
    else:
        if args.host != "127.0.0.1" and not (args.cert and args.key):
            parser.error("非 loopback 监听必须配置 TLS，或通过反向代理转发 loopback")
        uvicorn.run(
            create_app(settings),
            host=args.host,
            port=args.port,
            ssl_certfile=args.cert,
            ssl_keyfile=args.key,
            ws_max_size=2_010_000,
            ws_max_queue=2,
            access_log=False,
        )
