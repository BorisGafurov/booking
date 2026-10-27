#!/usr/bin/env python3
"""Sync core_bot and database/service facades across bot_booking, bot_VK_booking, and bot_Max_booking."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARENT = ROOT.parent

TARGETS = [
    PARENT / "bot_VK_booking",
    PARENT / "bot_Max_booking",
]

FACADE_TEMPLATE = """\"\"\"{name} service facade redirecting to core_bot.services.{name}.\"\"\"

import sys
import core_bot.services.{name} as _mod

sys.modules[__name__] = _mod
"""

SERVICES_TO_PROXY = [
    "appointments",
    "masters",
    "masters_sync",
    "referrals",
    "client_import",
    "recommendations",
]


def sync():
    core_src = ROOT / "core_bot"
    if not core_src.exists():
        print(f"Error: {core_src} does not exist!")
        sys.exit(1)

    print("==================================================")
    print("SYNCING core_bot PACKAGE ACROSS REPOSITORIES")
    print("==================================================")

    for target in TARGETS:
        if not target.exists():
            print(f"Skipping {target.name} (directory not found)")
            continue

        print(f"\nProcessing target: {target.name}")

        # 1. Sync core_bot directory
        core_dst = target / "core_bot"
        if core_dst.exists():
            shutil.rmtree(core_dst)
        shutil.copytree(core_src, core_dst)
        print(f"  [✓] Copied core_bot -> {core_dst}")

        # 2. Sync database.py facade
        shutil.copy2(ROOT / "database.py", target / "database.py")
        print(f"  [✓] Copied database.py facade -> {target / 'database.py'}")

        # 3. Create service facades in target/services
        services_dir = target / "services"
        services_dir.mkdir(exist_ok=True)
        for s in SERVICES_TO_PROXY:
            facade_file = services_dir / f"{s}.py"
            facade_file.write_text(FACADE_TEMPLATE.format(name=s))
            print(f"  [✓] Written service facade -> {facade_file.name}")

        # 4. Copy test_migrations.py
        tests_dir = target / "tests"
        if tests_dir.exists():
            shutil.copy2(ROOT / "tests" / "test_migrations.py", tests_dir / "test_migrations.py")
            print(f"  [✓] Copied tests/test_migrations.py -> {tests_dir}")

        # 5. Copy scripts directory
        scripts_dst = target / "scripts"
        scripts_dst.mkdir(exist_ok=True)
        shutil.copy2(ROOT / "scripts" / "sync_core.py", scripts_dst / "sync_core.py")
        print(f"  [✓] Copied scripts/sync_core.py -> {scripts_dst}")

        # 6. Copy docs directory
        docs_src = ROOT / "docs"
        if docs_src.exists():
            docs_dst = target / "docs"
            docs_dst.mkdir(exist_ok=True)
            for doc_file in docs_src.glob("*.md"):
                shutil.copy2(doc_file, docs_dst / doc_file.name)
            print(f"  [✓] Copied docs/ -> {docs_dst}")

    print("\nSync completed successfully.")


if __name__ == "__main__":
    sync()
