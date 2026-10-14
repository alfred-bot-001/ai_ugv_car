"""Run the authenticated Pi agent or inspect the unconfigured media contract."""

import argparse
import json
from pathlib import Path

from .session import PiSessionController


def main() -> None:
    parser = argparse.ArgumentParser(description="AI TV Pet Raspberry Pi endpoint")
    parser.add_argument(
        "--capabilities", action="store_true", help="print current media capabilities"
    )
    parser.add_argument(
        "--config", type=Path, help="run authenticated device agent with private JSON config"
    )
    args = parser.parse_args()
    if args.config:
        from .agent import run_agent

        run_agent(args.config)
        return
    if not args.capabilities:
        parser.error("use --capabilities or --config")
    print(json.dumps(PiSessionController().capabilities(), ensure_ascii=False))


if __name__ == "__main__":
    main()
