from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
import urllib3
from requests.auth import HTTPBasicAuth


DEFAULT_URL = "https://parsers360.ru:10443/enablers-api/api/v2/parametrized"
DEFAULT_TIMEOUT_SECONDS = 300
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(ENV_PATH)


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe the Parsers360 v2 endpoint.")
    parser.add_argument("--url", default=os.getenv("PARSERS360_API_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.getenv("PARSERS360_TOKEN"))
    parser.add_argument("--basic-user", default=first_env("PARSERS360_BASIC_USER", "PARSERS360_USER"))
    parser.add_argument(
        "--basic-password",
        default=first_env("PARSERS360_BASIC_PASSWORD", "PARSERS360_PASSWORD"),
    )
    parser.add_argument("--service", default="parser")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--page", type=int, default=4)
    parser.add_argument("--interval", default="17-06-2026to18-06-2026")
    parser.add_argument(
        "--summary",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--company",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--verify-ssl",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    return parser.parse_args()


def build_params(args: argparse.Namespace) -> dict[str, str]:
    params = {
        "service": args.service,
        "limit": str(args.limit),
        "page": str(args.page),
        "summary": "true" if args.summary else "false",
        "company": "true" if args.company else "false",
        "token": args.token,
        "interval": args.interval,
    }
    return params


def main() -> int:
    args = parse_args()
    if not args.token:
        print("Missing token. Pass --token or set PARSERS360_TOKEN.", file=sys.stderr)
        return 2
    if not args.basic_user or not args.basic_password:
        print(
            "Missing Basic Auth credentials. Pass --basic-user/--basic-password or set env vars.",
            file=sys.stderr,
        )
        return 2

    if not args.verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    params = build_params(args)

    try:
        response = requests.post(
            args.url,
            params=params,
            auth=HTTPBasicAuth(args.basic_user, args.basic_password),
            headers={"accept": "application/json"},
            timeout=args.timeout,
            verify=args.verify_ssl,
        )
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    print(f"status={response.status_code}")
    print(f"url={response.url}")
    print(response.text)

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
