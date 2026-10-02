"""Point d'entrée en ligne de commande : `clipforge serve`."""

from __future__ import annotations

import argparse
import logging

from clipforge import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clipforge", description="Clipping automatique TikTok")
    parser.add_argument("--version", action="version", version=f"Clipforge {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="Lance le dashboard, le worker et la surveillance")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        uvicorn.run("clipforge.web.app:app_factory", factory=True, host=args.host, port=args.port)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
