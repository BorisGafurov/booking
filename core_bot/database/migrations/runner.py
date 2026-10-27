import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)


def ensure_migration_table(conn) -> None:
    """Create schema_migrations table if it doesn't already exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(64) PRIMARY KEY,
            name VARCHAR(128) NOT NULL,
            applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


def get_applied_migrations(conn) -> set[str]:
    """Return the set of already applied migration version strings."""
    ensure_migration_table(conn)
    cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
    rows = cursor.fetchall()
    applied = set()
    for row in rows:
        if isinstance(row, (tuple, list)):
            applied.add(str(row[0]))
        elif hasattr(row, "__getitem__"):
            applied.add(str(row["version"]))
    return applied


def get_available_migrations(versions_dir: Path | None = None) -> list[tuple[str, str, Any]]:
    """
    Discover all migration modules in the versions directory, sorted by version name.
    Returns list of tuples: (version, name, module).
    """
    if versions_dir is None:
        versions_dir = Path(__file__).resolve().parent / "versions"

    if not versions_dir.exists():
        return []

    migration_files = sorted(
        [
            f for f in versions_dir.glob("*.py")
            if not f.name.startswith("__") and not f.name.startswith(".")
        ],
        key=lambda p: p.stem,
    )

    migrations = []
    for f in migration_files:
        stem = f.stem
        module_path = f"core_bot.database.migrations.versions.{stem}"
        try:
            if module_path in sys.modules:
                mod = importlib.reload(sys.modules[module_path])
            else:
                mod = importlib.import_module(module_path)
        except Exception:
            # Fallback for dynamic loading if not in sys.path
            spec = importlib.util.spec_from_file_location(stem, f)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            else:
                continue

        version = getattr(mod, "VERSION", stem)
        name = getattr(mod, "NAME", stem)
        migrations.append((version, name, mod))

    return migrations


def run_migrations(conn, versions_dir: Path | None = None) -> list[str]:
    """
    Run all pending migrations in order.
    Returns a list of newly applied version strings.
    """
    ensure_migration_table(conn)
    applied = get_applied_migrations(conn)
    available = get_available_migrations(versions_dir)

    executed = []
    for version, name, mod in available:
        if version not in applied:
            logger.info("Applying database migration [%s]: %s", version, name)
            if hasattr(mod, "apply"):
                mod.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (version, name),
            )
            conn.commit()
            executed.append(version)
            logger.info("Successfully applied database migration [%s]", version)

    return executed


def rollback_migration(conn, target_version: str, versions_dir: Path | None = None) -> bool:
    """
    Rollback a specific migration if its module defines a rollback function.
    """
    ensure_migration_table(conn)
    applied = get_applied_migrations(conn)
    if target_version not in applied:
        logger.warning("Migration [%s] has not been applied, cannot rollback.", target_version)
        return False

    available = get_available_migrations(versions_dir)
    target_mod = None
    for version, _, mod in available:
        if version == target_version:
            target_mod = mod
            break

    if not target_mod or not hasattr(target_mod, "rollback"):
        logger.warning("Migration [%s] does not support rollback.", target_version)
        return False

    logger.info("Rolling back migration [%s]...", target_version)
    target_mod.rollback(conn)
    conn.execute("DELETE FROM schema_migrations WHERE version = %s", (target_version,))
    conn.commit()
    logger.info("Successfully rolled back migration [%s].", target_version)
    return True


def get_migration_status(conn, versions_dir: Path | None = None) -> list[dict[str, Any]]:
    """
    Return status details for all available migrations.
    """
    ensure_migration_table(conn)
    cursor = conn.execute("SELECT version, applied_at FROM schema_migrations")
    applied_map = {}
    for row in cursor.fetchall():
        if isinstance(row, (tuple, list)):
            applied_map[str(row[0])] = row[1]
        elif hasattr(row, "__getitem__"):
            applied_map[str(row["version"])] = row["applied_at"]

    available = get_available_migrations(versions_dir)
    result = []
    for version, name, _ in available:
        result.append({
            "version": version,
            "name": name,
            "applied": version in applied_map,
            "applied_at": applied_map.get(version),
        })
    return result


if __name__ == "__main__":
    from core_bot.database.connection import get_connection

    with get_connection() as c:
        if len(sys.argv) > 1 and sys.argv[1] == "--status":
            statuses = get_migration_status(c)
            print("--- DATABASE MIGRATION STATUS ---")
            for s in statuses:
                status_str = "APPLIED" if s["applied"] else "PENDING"
                print(f"[{status_str}] {s['version']} - {s['name']} (applied_at: {s['applied_at']})")
        else:
            newly_applied = run_migrations(c)
            if newly_applied:
                print(f"Applied migrations: {', '.join(newly_applied)}")
            else:
                print("Database is up to date. No pending migrations.")
