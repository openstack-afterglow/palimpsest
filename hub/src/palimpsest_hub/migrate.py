from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import JSON, MetaData, Table, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

# Keep the complete destination metadata available to offline migration callers.
from palimpsest_hub import image_models, instance_models, native_models  # noqa: F401
from palimpsest_hub.image_models import LEGACY_JOB_VISIBILITY

_TABLES = (
    "palimpsest_hub_layers",
    "palimpsest_hub_layer_access",
    "palimpsest_hub_uploads",
    "palimpsest_image_exports",
    "palimpsest_image_export_delegations",
    "palimpsest_hub_builds",
    "palimpsest_worker_claims",
    "palimpsest_hub_build_contexts",
    "palimpsest_hub_image_jobs",
    "palimpsest_hub_publication_payloads",
    "palimpsest_hub_layer_nodes",
    "palimpsest_hub_published_images",
    "palimpsest_hub_image_memberships",
    "palimpsest_native_instances",
    "palimpsest_hub_instances",
    "palimpsest_runtime_blob_references",
    "palimpsest_package_namespaces",
    "palimpsest_packages",
    "palimpsest_package_keys",
    "palimpsest_package_versions",
    "palimpsest_package_blob_references",
    "palimpsest_package_tags",
    "palimpsest_package_uploads",
    "palimpsest_package_caches",
)


class MigrationError(RuntimeError):
    pass


async def _reflect(connection: AsyncConnection, table_name: str) -> Table:
    def reflect(sync_connection) -> Table:
        metadata = MetaData()
        return Table(table_name, metadata, autoload_with=sync_connection)

    return await connection.run_sync(reflect)


async def _row_count(connection: AsyncConnection, table: Table) -> int:
    return int((await connection.scalar(select(func.count()).select_from(table))) or 0)


def _ordered_layer_nodes(rows: list[dict]) -> list[dict]:
    """Insert parents before children without trusting database scan order."""
    nodes = {(row["project_id"], row["id"]): row for row in rows}
    children = defaultdict(list)
    ready = deque()
    for key, row in nodes.items():
        parent = row["parent_id"]
        if parent is None:
            ready.append(key)
        else:
            parent_key = (row["project_id"], parent)
            if parent_key not in nodes:
                raise MigrationError("source logical layer parent is missing")
            children[parent_key].append(key)
    ordered = []
    while ready:
        key = ready.popleft()
        ordered.append(nodes[key])
        ready.extend(children[key])
    if len(ordered) != len(rows):
        raise MigrationError("source logical layer graph is cyclic or duplicated")
    return ordered


def _reviewed_image_rows(table: Table, rows: list[dict], mapping: list[dict], used: set[int]) -> list[dict]:
    """Apply only reviewed exact-source-PK ACL facts, never infer an author.

    Mapping JSON is a list of {"table": <name>, "key": {<source PK>: <value>},
    "values": {"created_by": <proven user>}} records. For an intermediate
    publication schema, visibility and reuse_projects_json may also be supplied.
    Known source facts cannot be overwritten; null/blank creators are unknown.
    Context creators without this evidence require explicit operator quarantine.
    """
    allowed = {
        "palimpsest_hub_build_contexts": {"created_by"},
        "palimpsest_hub_publication_payloads": {"created_by"},
        "palimpsest_hub_image_jobs": set(),
        "palimpsest_hub_published_images": {"created_by", "visibility"},
        "palimpsest_hub_image_memberships": {"reuse_projects_json"},
    }
    if table.name not in allowed:
        return rows
    primary = {column.name for column in table.primary_key}
    for index, entry in enumerate(mapping):
        if entry["table"] != table.name:
            continue
        if not primary or set(entry["key"]) != primary:
            raise MigrationError("reviewed mapping must identify the complete exact source primary key")
        if not entry["values"] or not set(entry["values"]).issubset(allowed[table.name]):
            raise MigrationError("reviewed mapping contains unsupported ACL values")
        matches = [row for row in rows if all(row.get(key) == value for key, value in entry["key"].items())]
        if len(matches) != 1:
            raise MigrationError("reviewed mapping must match exactly one source row")
        row = matches[0]
        for key, value in entry["values"].items():
            current = row.get(key)
            unknown_creator = key == "created_by" and isinstance(current, str) and not current.strip()
            if current is not None and not unknown_creator:
                raise MigrationError("reviewed mapping cannot overwrite a known source fact")
            if key == "created_by" and (not isinstance(value, str) or not value.strip() or len(value) > 128):
                raise MigrationError("reviewed mapping creator must be a nonempty bounded user identity")
            if key == "visibility" and (not isinstance(value, str) or value not in {"private", "internal", "public"}):
                raise MigrationError("reviewed mapping visibility is invalid")
            if key == "reuse_projects_json" and (
                not isinstance(value, list)
                or any(not isinstance(project, str) or not project.strip() or len(project) > 64 for project in value)
            ):
                raise MigrationError("reviewed mapping reuse ceilings are invalid")
            row[key] = value
        used.add(index)
    for row in rows:
        # Pre-publication jobs were project-wide execution receipts, including
        # legitimate null creators. Do not turn them into private/catalog images.
        if table.name == "palimpsest_hub_image_jobs":
            if "visibility" not in table.c:
                row["visibility"] = LEGACY_JOB_VISIBILITY
            elif row.get("visibility") not in {"private", "internal", "public"}:
                raise MigrationError("source image-job visibility is invalid")
            continue
        if table.name in {
            "palimpsest_hub_build_contexts",
            "palimpsest_hub_publication_payloads",
            "palimpsest_hub_published_images",
        }:
            creator = row.get("created_by")
            if not isinstance(creator, str) or not creator.strip() or len(creator) > 128:
                raise MigrationError(
                    "legacy image creator requires a reviewed exact-row mapping or operator quarantine"
                )
        if table.name == "palimpsest_hub_published_images" and row.get("visibility") not in {
            "private",
            "internal",
            "public",
        }:
            raise MigrationError("legacy image visibility requires a reviewed exact-row mapping")
        if table.name == "palimpsest_hub_image_memberships":
            ceilings = row.get("reuse_projects_json")
            if isinstance(ceilings, str):
                # MariaDB reflects its JSON alias as LONGTEXT. Validate decoded
                # policy facts, then bind for the destination's actual SQL type.
                try:
                    ceilings = json.loads(ceilings)
                except json.JSONDecodeError:
                    raise MigrationError("source image reuse ceilings are invalid JSON") from None
            if not isinstance(ceilings, list) or any(
                not isinstance(project, str) or not project.strip() or len(project) > 64 for project in ceilings
            ):
                raise MigrationError("legacy image reuse ceilings require a reviewed exact-row mapping")
            row["reuse_projects_json"] = ceilings
    return rows


async def migrate(
    source_url: str,
    destination_url: str,
    *,
    dry_run: bool = False,
    reviewed_mapping: list[dict] | None = None,
) -> dict[str, int]:
    """Offline copy into an initialized empty destination after backup/review."""
    if make_url(source_url) == make_url(destination_url):
        raise MigrationError("source and destination databases must differ")
    mapping = reviewed_mapping if reviewed_mapping is not None else []
    if not isinstance(mapping, list) or any(
        not isinstance(entry, dict)
        or set(entry) != {"table", "key", "values"}
        or not isinstance(entry["table"], str)
        or not isinstance(entry["key"], dict)
        or not isinstance(entry["values"], dict)
        for entry in mapping
    ):
        raise MigrationError("reviewed mapping must be a JSON list of table/key/values records")
    used: set[int] = set()
    planned: list[tuple[Table, list[dict]]] = []

    source_engine = create_async_engine(source_url, pool_pre_ping=True)
    destination_engine = create_async_engine(destination_url, pool_pre_ping=True)
    copied: dict[str, int] = {}
    try:
        async with source_engine.connect() as source, destination_engine.begin() as destination:
            source_names = set(await source.run_sync(lambda connection: inspect(connection).get_table_names()))
            native_names = {name for name in _TABLES if name.startswith(("palimpsest_package_", "palimpsest_packages"))}
            present_native = source_names & native_names
            if present_native and present_native != native_names:
                raise MigrationError("source native package schema is incomplete")
            for table_name in _TABLES:
                destination_table = await _reflect(destination, table_name)
                destination_count = await _row_count(destination, destination_table)
                if destination_count:
                    raise MigrationError(f"destination table {table_name!r} is not empty")
                try:
                    source_table = await _reflect(source, table_name)
                except NoSuchTableError:
                    if table_name not in {
                        "palimpsest_hub_layer_access",
                        "palimpsest_hub_builds",
                        "palimpsest_worker_claims",
                        "palimpsest_image_export_delegations",
                        "palimpsest_hub_build_contexts",
                        "palimpsest_hub_image_jobs",
                        "palimpsest_hub_publication_payloads",
                        "palimpsest_hub_layer_nodes",
                        "palimpsest_hub_published_images",
                        "palimpsest_hub_image_memberships",
                        "palimpsest_native_instances",
                        "palimpsest_hub_instances",
                        "palimpsest_runtime_blob_references",
                    } and not table_name.startswith(("palimpsest_package_", "palimpsest_packages")):
                        raise
                    copied[table_name] = 0
                    continue
                rows = [dict(row) for row in (await source.execute(select(source_table))).mappings()]
                rows = _reviewed_image_rows(source_table, rows, mapping, used)
                if table_name == "palimpsest_hub_image_memberships" and not isinstance(
                    destination_table.c.reuse_projects_json.type, JSON
                ):
                    for row in rows:
                        row["reuse_projects_json"] = json.dumps(row["reuse_projects_json"])
                if table_name == "palimpsest_hub_layer_nodes":
                    rows = _ordered_layer_nodes(rows)
                if table_name == "palimpsest_hub_builds":
                    # Retired SQL-connected builder location is not HTTP attempt authority.
                    for row in rows:
                        row.pop("execution_node", None)
                copied[table_name] = len(rows)
                planned.append((destination_table, rows))
            if len(used) != len(mapping):
                raise MigrationError("reviewed mapping contains unused or unknown-table records")
            for destination_table, rows in planned:
                if rows and not dry_run:
                    await destination.execute(destination_table.insert(), rows)
        return copied
    finally:
        await source_engine.dispose()
        await destination_engine.dispose()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy Palimpsest Hub state into an initialized dedicated database")
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--destination-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reviewed-mapping",
        type=Path,
        help="Reviewed exact-source-PK ACL mapping JSON; stop services and back up both databases first",
    )
    return parser.parse_args(argv)


def run() -> None:
    args = parse_args()
    mapping = json.loads(args.reviewed_mapping.read_text()) if args.reviewed_mapping else None
    copied = asyncio.run(migrate(args.source_url, args.destination_url, dry_run=args.dry_run, reviewed_mapping=mapping))
    mode = "would copy" if args.dry_run else "copied"
    print(f"{mode}: " + ", ".join(f"{table}={count}" for table, count in copied.items()))


if __name__ == "__main__":
    run()
