"""Phone actions use the existing Hermes Talk command tree."""

import json

from .policy import PhoneError


def setup_parser(commands):
    phone = commands.add_parser("phone", help="Optional native Hermes Talk Phone mode")
    phone.add_argument("phone_action", choices=["setup", "doctor", "bridge"])
    phone.add_argument(
        "--import-dograh",
        action="store_true",
        help="Import this profile's existing dedicated Dograh setup",
    )
    phone.add_argument(
        "--port", type=int, default=8787, help="Explicit bridge listener on loopback"
    )


def run(ctx, args):
    try:
        if ctx is None:
            raise PhoneError("Run Phone mode through the Hermes Talk plugin in Hermes.")
        if args.phone_action == "setup":
            if getattr(args, "import_dograh", False):
                from .migration import import_dograh

                result = import_dograh()
                print(json.dumps(result, indent=2))
                return 0
            from .setup import run as setup

            return setup(ctx)
        if args.phone_action == "doctor":
            from .doctor import cli_report, doctor

            result = doctor(ctx)
            print(json.dumps(cli_report(result), indent=2))
            return 0 if result.get("connection_ready") else 1
        from .bridge import serve

        serve(ctx, args)
        return 0
    except PhoneError as exc:
        print(f"Phone: {exc}")
        return 1
