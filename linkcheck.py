#!/usr/bin/env python3
"""Check Markdown links in a directory."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


MARKDOWN_LINK = re.compile(r"!?(?:\[[^\]]*\])\((?:<)?([^\s)>]+)(?:>)?(?:\s+[^)]*)?\)")
FILE_LINK = re.compile(r"file://[^\s)>'\"]+")
HTTP_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class BrokenLink:
    source: Path
    target: str
    reason: str


def extract_links(markdown: str) -> list[str]:
    links = MARKDOWN_LINK.findall(markdown)
    links.extend(FILE_LINK.findall(markdown))
    return list(dict.fromkeys(links))


def local_target(source: Path, target: str) -> Path:
    parsed = urlsplit(target)
    if parsed.scheme == "file":
        if parsed.netloc and parsed.netloc != "localhost":
            return Path("/") / parsed.netloc / unquote(parsed.path.lstrip("/"))
        return Path(unquote(parsed.path))
    return source.parent / unquote(parsed.path)


def check_http(url: str, timeout: float) -> str | None:
    headers = {"User-Agent": "linkcheck/1.0"}
    try:
        request = Request(url, method="HEAD", headers=headers)
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            return None if 200 <= status < 400 else f"HTTP {status}"
    except HTTPError as error:
        if error.code not in {405, 501}:
            return None if 200 <= error.code < 400 else f"HTTP {error.code}"
    except (URLError, TimeoutError, OSError):
        pass

    try:
        request = Request(url, method="GET", headers=headers)
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            return None if 200 <= status < 400 else f"HTTP {status}"
    except HTTPError as error:
        return None if 200 <= error.code < 400 else f"HTTP {error.code}"
    except (URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        return f"{type(reason).__name__}: {reason}"


def check_link(source: Path, target: str, timeout: float) -> str | None:
    parsed = urlsplit(target)
    if parsed.scheme in HTTP_SCHEMES:
        return check_http(target, timeout)
    if parsed.scheme in {"", "file"}:
        path = local_target(source, target)
        return None if path.exists() else "file not found"
    return None


def scan(folder: Path, timeout: float) -> list[BrokenLink]:
    broken: list[BrokenLink] = []
    for source in sorted(folder.rglob("*.md")):
        try:
            content = source.read_text(encoding="utf-8")
        except OSError as error:
            broken.append(BrokenLink(source, "<file>", f"cannot read source: {error}"))
            continue
        for target in extract_links(content):
            reason = check_link(source, target, timeout)
            if reason:
                broken.append(BrokenLink(source, target, reason))
    return broken


def print_report(broken: list[BrokenLink]) -> None:
    if not broken:
        print("All links are valid.")
        return
    print(f"Broken links: {len(broken)}")
    use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    for item in broken:
        prefix = "\033[31m-\033[0m" if use_color else "-"
        print(f"{prefix} {item.source}: {item.target} -> {item.reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check links in Markdown files.")
    parser.add_argument("folder", type=Path, help="folder to scan recursively")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        parser.error(f"not a directory: {args.folder}")
    broken = scan(args.folder, args.timeout)
    print_report(broken)
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())