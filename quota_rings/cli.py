"""Command line interface.

    python -m quota_rings serve   --config config.toml    # run the dashboard
    python -m quota_rings once    --config config.toml    # one snapshot to stdout
    python -m quota_rings check   --config config.toml    # health/debug, no secrets

``check`` is the health/debug command: it reports which credentials resolve,
where each one comes from (by reference only), and optionally probes the
vendors. It never prints a credential value.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .collector import Collector
from .config import ConfigError, Settings, load_config
from .secrets import REDACTOR
from .server import QuotaRingsServer, is_loopback

DEFAULT_CONFIG = "config.toml"


def _load(path: str) -> Settings:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _snapshot(collector: Collector) -> dict:
    try:
        return collector.get()
    except Exception as exc:  # noqa: BLE001
        print(f"collection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_once(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    payload = _snapshot(Collector(settings))
    print(json.dumps(REDACTOR.scrub(payload), indent=2, ensure_ascii=False))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    collector = Collector(settings)
    report: dict = {"config": settings.config_path, "accounts": collector.check()}

    if args.probe:
        payload = _snapshot(collector)
        report["snapshot"] = {
            "generated_at": payload["generated_at"],
            "accounts": [
                {
                    "id": a["id"],
                    "provider": a["provider"],
                    "ok": a["ok"],
                    "stale": a["stale"],
                    "windows": len(a["windows"]),
                    "extras": len(a["extras"]),
                    "ms": a["ms"],
                    "error": a["error"],
                }
                for a in payload["accounts"]
            ],
        }

    if args.json:
        print(json.dumps(REDACTOR.scrub(report), indent=2, ensure_ascii=False))
    else:
        print(f"config: {report['config']}")
        print(f"server: {settings.host}:{settings.port}  ttl={settings.ttl_seconds}s  "
              f"vendor_actions={'on' if settings.enable_vendor_actions else 'off'}")
        print()
        for entry in report["accounts"]:
            line = f"  {entry['id']:<24} {entry['provider']:<12} {entry['credential']}"
            print(line)
        if "snapshot" in report:
            print()
            for a in report["snapshot"]["accounts"]:
                state = "ok" if a["ok"] else "FAIL"
                if a["stale"]:
                    state = "stale"
                print(f"  {a['id']:<24} {state:<6} windows={a['windows']} {a['ms']}ms")
                if a["error"]:
                    print(f"      {a['error']}")

    failed = [e for e in report["accounts"] if not str(e["credential"]).startswith("resolved")]
    if args.probe:
        failed += [a["id"] for a in report.get("snapshot", {}).get("accounts", []) if not a["ok"]]
    return 1 if failed else 0


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    if not is_loopback(settings.host) and not settings.allow_remote_bind:
        print(
            f"refusing to bind {settings.host}: it is not a loopback address, so anyone who can "
            "reach this port can read the dashboard. Set server.allow_remote_bind = true in the "
            "config if you have put authentication in front of it.",
            file=sys.stderr,
        )
        return 2

    collector = Collector(settings)
    server = QuotaRingsServer(settings, collector)
    print(f"quota-rings on http://{settings.host}:{settings.port}")
    print(f"  accounts: {len(settings.accounts)}   refresh: {settings.ttl_seconds}s")
    print(f"  vendor quota resets: {'ENABLED' if settings.enable_vendor_actions else 'disabled'}")
    print("  credentials stay server-side; the browser receives percentages only")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quota-rings", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to the TOML config")

    serve = sub.add_parser("serve", help="run the dashboard")
    common(serve)
    serve.add_argument("--host", help="override server.host")
    serve.add_argument("--port", type=int, help="override server.port")
    serve.set_defaults(func=cmd_serve)

    once = sub.add_parser("once", help="collect one snapshot, print JSON, exit")
    common(once)
    once.set_defaults(func=cmd_once)

    check = sub.add_parser("check", help="health/debug report; never prints credentials")
    common(check)
    check.add_argument("--probe", action="store_true", help="also fetch the vendors once")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
