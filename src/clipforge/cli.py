"""Point d'entrée en ligne de commande : `clipforge serve`, `clipforge reset-password`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from clipforge import __version__


def _reset_password(email: str) -> int:
    from clipforge.config import get_settings
    from clipforge.db.session import make_engine, make_session_factory
    from clipforge.web import auth

    settings = get_settings()
    password = getpass.getpass("Nouveau mot de passe : ")
    if password != getpass.getpass("Confirmer : "):
        print("Les mots de passe ne correspondent pas.")
        return 1
    email = auth.normalize_email(email)
    error = auth.validate_registration(email, password, password, settings)
    if error:
        print(error)
        return 1
    with make_session_factory(make_engine(settings.db_path))() as db:
        if not auth.set_password(db, email, password):
            print("Aucun compte avec cet e-mail.")
            return 1
    print("Mot de passe changé. Toutes les sessions ont été fermées.")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clipforge", description="Clipping automatique TikTok")
    parser.add_argument("--version", action="version", version=f"Clipforge {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="Lance le dashboard, le worker et la surveillance")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    reset = sub.add_parser("reset-password", help="Change le mot de passe d'un compte")
    reset.add_argument("email")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        uvicorn.run("clipforge.web.app:app_factory", factory=True, host=args.host, port=args.port)
    elif args.cmd == "reset-password":
        sys.exit(_reset_password(args.email))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
