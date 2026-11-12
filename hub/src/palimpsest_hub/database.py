from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _recover_closed_transport_ping(engine: AsyncEngine) -> None:
    ping = engine.sync_engine.dialect.do_ping

    def do_ping(connection) -> bool:
        try:
            return ping(connection)
        except RuntimeError as exc:
            # asyncmy lets uvloop's closed-socket RuntimeError escape its DBAPI adapter.
            # Report only that disconnected transport to SQLAlchemy's pool pre-ping.
            message = str(exc)
            if "TCPTransport closed=True" not in message or "handler is closed" not in message:
                raise
            return False

    engine.sync_engine.dialect.do_ping = do_ping


def init_db(
    database_url: str,
    *,
    pool_size: int,
    max_overflow: int,
    connect_timeout: int,
    read_timeout: int,
    pool_timeout: int,
    unhealthy_seconds: int,
) -> None:
    del unhealthy_seconds
    global _engine, _session_factory
    if _engine is not None:
        return
    connect_args = {}
    if make_url(database_url).get_driver_name() == "asyncmy":
        # TCP connect timeout does not bound asyncmy's handshake or response reads.
        connect_args = {"connect_timeout": connect_timeout, "read_timeout": read_timeout}
    _engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        connect_args=connect_args,
    )
    if _engine.sync_engine.dialect.driver == "asyncmy":
        _recover_closed_transport_ping(_engine)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Palimpsest Hub database is unavailable")
    async with factory() as session:
        yield session


def _image_schema_upgrade(connection) -> None:
    """Upgrade only reviewed image schemas, before creating dependent tables.

    Operators must stop every API/worker and take a restorable database backup
    first. SQLite runs in BEGIN IMMEDIATE; MySQL/MariaDB DDL implicitly commits,
    so its rollback is restoring that backup, not a SQL transaction rollback.
    Unknown creators must be proved offline or their rows explicitly quarantined
    by the operator; neither project IDs nor queued-job references prove authorship.
    """
    from palimpsest_hub.image_models import LEGACY_JOB_VISIBILITY, PalimpsestHubBuildContext
    from palimpsest_hub.models import Base

    inspector = inspect(connection)
    names = set(inspector.get_table_names())
    contexts = "palimpsest_hub_build_contexts"
    jobs = "palimpsest_hub_image_jobs"
    rebuild_contexts = False
    add_visibility = False
    dialect = connection.dialect.name
    if dialect not in {"sqlite", "mysql", "mariadb"}:
        raise RuntimeError("schema bootstrap supports only SQLite or MySQL/MariaDB")

    # Preflight everything before any DDL (especially implicitly committed DDL).
    if contexts in names:
        columns = {item["name"]: item for item in inspector.get_columns(contexts)}
        expected = {"project_id", "digest", "size_bytes", "created_by", "created_at"}
        if set(columns) != expected:
            raise RuntimeError("unrecognized build-context schema; explicit offline operator migration required")
        primary = inspector.get_pk_constraint(contexts)["constrained_columns"]
        if primary not in (["project_id", "digest"], ["project_id", "digest", "created_by"]):
            raise RuntimeError("unrecognized build-context primary key; explicit offline operator migration required")
        unknown = connection.execute(
            text(f"SELECT 1 FROM {contexts} WHERE created_by IS NULL OR TRIM(created_by) = '' LIMIT 1")
        ).first()
        if unknown:
            raise RuntimeError(
                "build-context creator is unproven; stop services, back up, then explicitly quarantine "
                "those rows or apply a reviewed exact-creator mapping offline"
            )
        rebuild_contexts = primary != ["project_id", "digest", "created_by"] or columns["created_by"]["nullable"]
        if rebuild_contexts:
            incoming = [
                table
                for table in names
                for foreign in inspector.get_foreign_keys(table)
                if foreign["referred_table"] == contexts
            ]
            if incoming or inspector.get_indexes(contexts) or inspector.get_unique_constraints(contexts):
                raise RuntimeError("build-context dependencies require an explicit offline operator migration")
            if (
                dialect == "sqlite"
                and connection.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=:table LIMIT 1"),
                    {"table": contexts},
                ).first()
            ):
                raise RuntimeError("build-context triggers require an explicit offline operator migration")
            if "__palimpsest_upgrade_build_contexts" in names:
                raise RuntimeError("unfinished build-context upgrade requires operator recovery from backup")
    if jobs in names:
        columns = {item["name"]: item for item in inspector.get_columns(jobs)}
        add_visibility = "visibility" not in columns
        if not add_visibility and columns["visibility"]["nullable"]:
            raise RuntimeError("nullable image-job visibility requires an explicit offline operator migration")
    for name in (
        "palimpsest_hub_publication_payloads",
        "palimpsest_hub_layer_nodes",
        "palimpsest_hub_published_images",
        "palimpsest_hub_image_memberships",
    ):
        if name not in names:
            continue
        table = Base.metadata.tables[name]
        columns = {item["name"] for item in inspector.get_columns(name)}
        primary = inspector.get_pk_constraint(name)["constrained_columns"]
        if columns != set(table.columns.keys()) or primary != [column.name for column in table.primary_key]:
            raise RuntimeError(f"unrecognized {name} schema; explicit offline operator migration required")

    # Never let create_all attach new FKs to a legacy incompatible parent key.
    for table in Base.metadata.tables.values():
        for foreign in table.foreign_key_constraints:
            elements = list(foreign.elements)
            parent = elements[0].column.table.name
            if parent not in names:
                continue
            target = [element.column.name for element in elements]
            keys = [inspector.get_pk_constraint(parent)["constrained_columns"]]
            keys.extend(item["column_names"] for item in inspector.get_unique_constraints(parent))
            keys.extend(item["column_names"] for item in inspector.get_indexes(parent) if item["unique"])
            if target not in keys:
                raise RuntimeError(
                    f"incompatible legacy key on {parent}; explicit offline operator migration required before bootstrap"
                )

    if rebuild_contexts:
        if dialect in {"mysql", "mariadb"}:
            connection.execute(
                text(
                    f"ALTER TABLE {contexts} MODIFY COLUMN created_by VARCHAR(128) NOT NULL, "
                    "DROP PRIMARY KEY, ADD PRIMARY KEY (project_id, digest, created_by)"
                )
            )
        else:
            # SQLite cannot alter a PK. Copy only exact known creators, retaining
            # every value; the explicit transaction also rolls back table DDL.
            replacement = PalimpsestHubBuildContext.__table__.to_metadata(
                MetaData(), name="__palimpsest_upgrade_build_contexts"
            )
            replacement.create(connection)
            fields = "project_id, digest, size_bytes, created_by, created_at"
            connection.execute(text(f"INSERT INTO {replacement.name} ({fields}) SELECT {fields} FROM {contexts}"))
            connection.execute(text(f"DROP TABLE {contexts}"))
            connection.execute(text(f"ALTER TABLE {replacement.name} RENAME TO {contexts}"))
    if add_visibility:
        # Historical jobs were project-wide. "private" would silently reinterpret
        # old ACLs; this does not synthesize a catalog image or a missing creator.
        connection.execute(
            text(f"ALTER TABLE {jobs} ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT '{LEGACY_JOB_VISIBILITY}'")
        )


async def create_schema() -> None:
    """Offline bootstrap/upgrade: stop all consumers and back up before calling."""
    if _engine is None:
        raise RuntimeError("database is not initialized")
    # Register the independent image-job and instance ledgers before cold bootstrap.
    from palimpsest_hub import (  # noqa: F401
        image_key_models,
        image_models,
        instance_lifecycle_models,
        instance_models,
        native_models,
    )
    from palimpsest_hub.models import Base

    async with _engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            # pysqlite/aiosqlite legacy transaction mode does not BEGIN for DDL.
            await connection.execute(text("BEGIN IMMEDIATE"))
        await connection.run_sync(_image_schema_upgrade)
        await connection.run_sync(Base.metadata.create_all)
        # create_all creates new tables but intentionally does not evolve existing ones.
        # Bootstrap runs with every API/worker stopped; never backfill a live owner.
        columns = await connection.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("palimpsest_hub_builds")}
        )
        for name, definition in (
            ("lease_expires_at", "DATETIME(6)" if connection.dialect.name in {"mysql", "mariadb"} else "DATETIME"),
        ):
            if name not in columns:
                await connection.execute(text(f"ALTER TABLE palimpsest_hub_builds ADD COLUMN {name} {definition} NULL"))
        key_columns = await connection.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("palimpsest_package_keys")}
        )
        if "admin_issuer_context" not in key_columns:
            await connection.execute(
                text("ALTER TABLE palimpsest_package_keys ADD COLUMN admin_issuer_context JSON NULL")
            )
        for table, column in (
            ("palimpsest_package_uploads", "key_id"),
            ("palimpsest_package_versions", "pushed_key_id"),
        ):
            metadata = await connection.run_sync(lambda sync, table=table: inspect(sync).get_columns(table))
            current = next(item for item in metadata if item["name"] == column)
            if current["nullable"]:
                continue
            if connection.dialect.name not in {"mysql", "mariadb"}:
                raise RuntimeError(
                    "Existing non-null package key audit columns require an offline MySQL/MariaDB upgrade"
                )
            # Existing key references and FK constraints remain intact; only real
            # human administrator uploads may persist a null credential audit ID.
            await connection.execute(text(f"ALTER TABLE {table} MODIFY COLUMN {column} CHAR(32) NULL"))


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
