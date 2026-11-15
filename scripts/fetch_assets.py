#!/usr/bin/env python3
"""Download all front-end vendor assets locally (nothing is loaded from a CDN at runtime)
and build the Tailwind CSS bundle.

  * Tailwind CSS standalone CLI (no Node.js needed), SHA-256 verified -> app/static/css/app.css
  * Line Awesome icon font by Icons8 (code MIT, fonts SIL OFL 1.1)
  * Icons8 "Fluency" colour icons (free with attribution - credited on the About page)
  * Inter + JetBrains Mono web fonts (SIL OFL 1.1) via Fontsource

Usage:  python scripts/fetch_assets.py [--force] [--skip-tailwind] [--css-only]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
VENDOR = STATIC / "vendor"
TOOLS = ROOT / ".runtime" / "tools"
TAILWIND_VERSION = "v4.3.3"
LINE_AWESOME = "https://cdn.jsdelivr.net/npm/line-awesome@1.3.0/dist/line-awesome"
FONTSOURCE = "https://cdn.jsdelivr.net/fontsource/fonts"
ICONS8 = "https://img.icons8.com/fluency/96"
UA = {"User-Agent": "cte-asset-fetcher/1.0"}

ICONS = [
    "brain", "bug", "source-code", "idea", "trophy", "shield", "settings", "chat", "combo-chart", "lock", "unlock",
    "processor", "ok", "error", "rocket", "puzzle", "memory-slot", "search", "hammer", "question-mark", "fire-element",
    "artificial-intelligence", "console", "opened-folder", "upload", "visible", "graduation-cap", "target", "medal", "light-on",
    "crescent-moon", "sun", "robot-2", "microphone", "speaker", "warning-shield", "checked", "cancel", "inspection",
    "stopwatch", "user-male-circle", "user-female-circle", "physics", "test-tube", "workflow", "classroom", "survey",
]
FONTS = [("inter", w) for w in (400, 500, 600, 700, 800)] + [("jetbrains-mono", w) for w in (400, 500, 700)]


def fetch(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
        return response.read()


def save(url: str, dest: Path, *, force: bool = False) -> bool:
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return True
    try:
        data = fetch(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  ! failed {url}: {exc}")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True


def tailwind_asset() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if system == "Windows":
        return "tailwindcss-windows-x64.exe"
    if system == "Darwin":
        return f"tailwindcss-macos-{arch}"
    return f"tailwindcss-linux-{arch}"


def ensure_tailwind(force: bool) -> Path | None:
    name = tailwind_asset()
    binary = TOOLS / f"{TAILWIND_VERSION}-{name}"
    if binary.exists() and not force:
        return binary
    base = f"https://github.com/tailwindlabs/tailwindcss/releases/download/{TAILWIND_VERSION}"
    print(f"- Tailwind CLI {TAILWIND_VERSION} ({name})")
    try:
        sums = fetch(f"{base}/sha256sums.txt").decode("utf-8", "replace")
        data = fetch(f"{base}/{name}", timeout=300)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  ! could not download Tailwind CLI: {exc}")
        return None
    expected = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(name)), None)
    digest = hashlib.sha256(data).hexdigest()
    if expected and expected.lower() != digest:
        print(f"  ! checksum mismatch for {name} (expected {expected}, got {digest}) - refusing to use it")
        return None
    print(f"  sha256 {'verified' if expected else 'not published - skipped'}: {digest[:16]}...")
    TOOLS.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(data)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def build_css(binary: Path) -> bool:
    css_dir = STATIC / "css"
    cmd = [str(binary), "-i", str(css_dir / "input.css"), "-o", str(css_dir / "app.css"), "--minify"]
    print("- building app/static/css/app.css with Tailwind")
    result = subprocess.run(cmd, cwd=str(css_dir), capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        print(result.stdout[-2000:], result.stderr[-2000:])
        return False
    print("  done:", (css_dir / "app.css").stat().st_size // 1024, "KB")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download everything")
    parser.add_argument("--skip-tailwind", action="store_true", help="do not download / run the Tailwind CLI")
    parser.add_argument("--css-only", action="store_true", help="only (re)build the CSS")
    args = parser.parse_args()
    VENDOR.mkdir(parents=True, exist_ok=True)

    if not args.css_only:
        print("- Line Awesome (Icons8)")
        save(f"{LINE_AWESOME}/css/line-awesome.min.css", VENDOR / "line-awesome" / "css" / "line-awesome.min.css", force=args.force)
        for font in ("la-solid-900", "la-regular-400", "la-brands-400"):
            for ext in ("woff2", "woff", "ttf"):
                save(f"{LINE_AWESOME}/fonts/{font}.{ext}", VENDOR / "line-awesome" / "fonts" / f"{font}.{ext}", force=args.force)

        print("- Icons8 Fluency colour icons")
        available = []
        for icon in ICONS:
            if save(f"{ICONS8}/{icon}.png", VENDOR / "icons8" / f"{icon}.png", force=args.force):
                available.append(icon)
        print(f"  {len(available)}/{len(ICONS)} icons available")

        print("- Fonts (Inter, JetBrains Mono)")
        for family, weight in FONTS:
            save(f"{FONTSOURCE}/{family}@latest/latin-{weight}-normal.woff2", VENDOR / "fonts" / f"{family}-{weight}.woff2", force=args.force)

        (VENDOR / "manifest.json").write_text(json.dumps({"icons8": available, "tailwind": TAILWIND_VERSION}, indent=1), encoding="utf-8")

    if not args.skip_tailwind:
        binary = ensure_tailwind(args.force)
        if binary is None or not build_css(binary):
            print("  ! Tailwind build failed - the committed app/static/css/app.css (if present) will be used")
            return 0 if (STATIC / "css" / "app.css").exists() else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
