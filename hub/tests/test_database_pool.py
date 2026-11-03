from __future__ import annotations

import asyncio
import fcntl
import hashlib
import multiprocessing
import os
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from asyncmy.errors import OperationalError as AsyncmyOperationalError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from palimpsest_hub.database import _recover_closed_transport_ping


@pytest.mark.parametrize("timeout", [0, 121])
def test_database_read_timeout_rejects_unbounded_values(timeout):
    from palimpsest_hub.config import DatabaseSettings

    with pytest.raises(ValidationError):
        DatabaseSettings(
            database_url="mysql+asyncmy://synthetic:synthetic@127.0.0.1/hub", database_read_timeout=timeout
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("finite_reads", [False, True], ids=["connect-timeout-only", "bounded-hub-reads"])
async def test_real_asyncmy_silent_handshake_read_lifetime(monkeypatch, finite_reads):
    """A real asyncmy/SQLAlchemy connection must terminate without an outer cancellation."""
    from palimpsest_hub import database
    from palimpsest_hub.config import DatabaseSettings

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    accepted = asyncio.Event()
    peer_closed = asyncio.Event()
    peers = set()
    handlers = []
    received = []

    async def silent_peer(reader, writer):
        # Accept TCP but send no MySQL greeting: connect_timeout alone is insufficient.
        peers.add(writer)
        handlers.append(asyncio.current_task())
        accepted.set()
        try:
            received.append(await reader.read())
        finally:
            writer.close()
            await writer.wait_closed()
            peers.remove(writer)
            peer_closed.set()

    server = await asyncio.start_server(silent_peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = DatabaseSettings(
        database_url=f"mysql+asyncmy://synthetic:synthetic@127.0.0.1:{port}/hub",
        database_read_timeout=1,
    )
    if finite_reads:
        database.init_db(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            connect_timeout=settings.database_connect_timeout,
            read_timeout=settings.database_read_timeout,
            pool_timeout=settings.database_pool_timeout,
            unhealthy_seconds=settings.database_unhealthy_seconds,
        )
        engine = database._engine
    else:
        # Previous Hub policy, through the real driver; no mocked argument capture.
        engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": settings.database_connect_timeout},
        )

    async def connect():
        async with engine.connect():
            raise AssertionError("a silent MySQL peer cannot complete authentication")

    task = asyncio.create_task(connect())
    try:
        await asyncio.wait_for(accepted.wait(), timeout=3)
        done, pending = await asyncio.wait({task}, timeout=3 if finite_reads else 1.5)
        if finite_reads:
            # asyncio.wait does not cancel: this must be the driver's bounded failure.
            assert done == {task} and not pending, "asyncmy read outlived the outer deadline"
            assert not task.cancelled()
            with pytest.raises(OperationalError) as raised:
                await task
            assert isinstance(raised.value.orig, AsyncmyOperationalError)
        else:
            assert not done and pending == {task}, "connect_timeout unexpectedly bounded the handshake"
            assert not peer_closed.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # Both driver timeout and explicit control cancellation must close the real socket.
        await asyncio.wait_for(peer_closed.wait(), timeout=3)
        assert task.done() and not peers
        assert len(handlers) == 1 and received == [b""]
        await asyncio.wait_for(asyncio.gather(*handlers), timeout=3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
        for writer in tuple(peers):
            writer.close()
        await asyncio.wait_for(asyncio.gather(*handlers, return_exceptions=True), timeout=3)
        if finite_reads:
            await database.close_db()
        else:
            await engine.dispose()


@pytest.mark.asyncio
async def test_database_initialization_keeps_sqlite_adapter_clean(tmp_path, monkeypatch):
    from palimpsest_hub import database
    from palimpsest_hub.config import DatabaseSettings

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    settings = DatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'initialized.db'}")
    database.init_db(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_timeout=settings.database_connect_timeout,
        read_timeout=settings.database_read_timeout,
        pool_timeout=settings.database_pool_timeout,
        unhealthy_seconds=settings.database_unhealthy_seconds,
    )
    try:
        factory = database.get_session_factory()
        assert factory is not None
        async with factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await database.close_db()


@pytest.mark.asyncio
async def test_closed_transport_ping_reconnects_without_masking_unrelated_errors(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}", pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1

        original_ping = engine.sync_engine.dialect.do_ping
        failure = "unable to perform operation on <TCPTransport closed=True reading=False>; the handler is closed"

        def ping(connection):
            if failure:
                raise RuntimeError(failure)
            return original_ping(connection)

        engine.sync_engine.dialect.do_ping = ping
        _recover_closed_transport_ping(engine)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1

        failure = "unrelated application error"
        with pytest.raises(RuntimeError, match="unrelated application error"):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_upgrades_legacy_build_ownership_without_inventing_an_owner(tmp_path, monkeypatch):
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub import database
    from palimpsest_hub.models import PalimpsestHubBuild, PalimpsestWorkerLease

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE palimpsest_hub_builds (id CHAR(36) PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO palimpsest_hub_builds (id) VALUES ('retained')"))
        await database.create_schema()
        await database.create_schema()  # Offline bootstrap is repeatable.
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {column["name"] for column in inspect(sync).get_columns(PalimpsestHubBuild.__tablename__)}
            )
            assert "lease_expires_at" in columns and "execution_node" not in columns
            row = (await connection.execute(text("SELECT id, lease_expires_at FROM palimpsest_hub_builds"))).one()
            assert tuple(row) == ("retained", None)
            for table, key_column in (
                ("palimpsest_package_uploads", "key_id"),
                ("palimpsest_package_versions", "pushed_key_id"),
            ):
                metadata = await connection.run_sync(lambda sync, table=table: inspect(sync).get_columns(table))
                assert next(column for column in metadata if column["name"] == key_column)["nullable"] is True
            key_columns = await connection.run_sync(
                lambda sync: {column["name"]: column for column in inspect(sync).get_columns("palimpsest_package_keys")}
            )
            assert key_columns["admin_issuer_context"]["nullable"] is True
        async with async_sessionmaker(engine)() as session:
            assert await session.get(PalimpsestWorkerLease, "export-maintenance") is None
    finally:
        await engine.dispose()


# Frozen SQLite DDL compiled from v0.3.10 models.py (15 tables, including indexes).
# Kept literal: this fixture must not silently follow the current ORM metadata.
_RELEASE_0310_SQL = (
    """
    CREATE TABLE palimpsest_hub_builds (
        id CHAR(36) NOT NULL,
        project_id VARCHAR(64) NOT NULL,
        created_by VARCHAR(128),
        name VARCHAR(64) NOT NULL,
        recipe TEXT NOT NULL,
        recipe_digest VARCHAR(71) NOT NULL,
        base_digest VARCHAR(71) NOT NULL,
        layer_digests JSON NOT NULL,
        status VARCHAR(16) NOT NULL,
        output_digest VARCHAR(71),
        output_size_bytes BIGINT,
        error_code VARCHAR(32),
        lease_owner VARCHAR(128),
        lease_expires_at DATETIME,
        created_at DATETIME NOT NULL,
        started_at DATETIME,
        completed_at DATETIME,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE INDEX idx_palimpsest_builds_project_created ON palimpsest_hub_builds (project_id, created_at)
    """,
    """
    CREATE TABLE palimpsest_hub_layers (
        id INTEGER NOT NULL,
        blob_digest VARCHAR(71) NOT NULL,
        blob_md5 CHAR(32),
        size_bytes BIGINT NOT NULL,
        media_type VARCHAR(128) NOT NULL,
        disk_format VARCHAR(16),
        arch VARCHAR(16),
        os_variant VARCHAR(64),
        config_digest VARCHAR(71) NOT NULL,
        chain_id VARCHAR(71),
        parent_digest VARCHAR(71),
        name VARCHAR(64) NOT NULL,
        kind VARCHAR(16) NOT NULL,
        ubuntu_base VARCHAR(64),
        python_version VARCHAR(16),
        config_json JSON NOT NULL,
        project_id VARCHAR(64),
        is_published BOOLEAN DEFAULT '0' NOT NULL,
        created_by VARCHAR(128),
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (blob_digest)
    )
    """,
    """
    CREATE INDEX ix_palimpsest_hub_layers_blob_md5 ON palimpsest_hub_layers (blob_md5)
    """,
    """
    CREATE INDEX ix_palimpsest_hub_layers_chain_id ON palimpsest_hub_layers (chain_id)
    """,
    """
    CREATE INDEX ix_palimpsest_hub_layers_kind ON palimpsest_hub_layers (kind)
    """,
    """
    CREATE INDEX ix_palimpsest_hub_layers_name ON palimpsest_hub_layers (name)
    """,
    """
    CREATE INDEX ix_palimpsest_hub_layers_parent_digest ON palimpsest_hub_layers (parent_digest)
    """,
    """
    CREATE INDEX ix_palimpsest_hub_layers_project_id ON palimpsest_hub_layers (project_id)
    """,
    """
    CREATE TABLE palimpsest_hub_uploads (
        id CHAR(32) NOT NULL,
        declared_digest VARCHAR(71),
        received_bytes BIGINT NOT NULL,
        project_id VARCHAR(64),
        created_by VARCHAR(128),
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE INDEX ix_palimpsest_hub_uploads_project_id ON palimpsest_hub_uploads (project_id)
    """,
    """
    CREATE TABLE palimpsest_image_export_delegations (
        trust_id VARCHAR(64) NOT NULL,
        export_id CHAR(36),
        project_id VARCHAR(64) NOT NULL,
        trustor_user_id VARCHAR(64) NOT NULL,
        trustee_user_id VARCHAR(64) NOT NULL,
        role_names JSON NOT NULL,
        expires_at DATETIME NOT NULL,
        state VARCHAR(16) NOT NULL,
        cleanup_attempts INTEGER DEFAULT '0' NOT NULL,
        cleanup_next_at DATETIME,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        finished_at DATETIME,
        PRIMARY KEY (trust_id)
    )
    """,
    """
    CREATE INDEX idx_palimpsest_export_delegations_cleanup ON palimpsest_image_export_delegations (state, cleanup_next_at)
    """,
    """
    CREATE INDEX idx_palimpsest_export_delegations_export ON palimpsest_image_export_delegations (export_id, state)
    """,
    """
    CREATE TABLE palimpsest_image_exports (
        id CHAR(36) NOT NULL,
        project_id VARCHAR(64) NOT NULL,
        created_by VARCHAR(128),
        source_image_id VARCHAR(64) NOT NULL,
        source_name VARCHAR(255) NOT NULL,
        source_disk_format VARCHAR(16) NOT NULL,
        source_size_bytes BIGINT NOT NULL,
        source_virtual_size_bytes BIGINT,
        source_checksum VARCHAR(64),
        source_hash_algo VARCHAR(16),
        source_hash_value VARCHAR(128),
        source_updated_at VARCHAR(64),
        source_fingerprint CHAR(64) NOT NULL,
        artifact_key CHAR(64) NOT NULL,
        target_disk_format VARCHAR(16) NOT NULL,
        result_blob_digest VARCHAR(71),
        result_size_bytes BIGINT,
        status VARCHAR(16) DEFAULT 'queued' NOT NULL,
        progress_pct INTEGER DEFAULT '0' NOT NULL,
        error_code VARCHAR(64),
        error_message TEXT,
        attempts INTEGER DEFAULT '0' NOT NULL,
        next_at DATETIME NOT NULL,
        lease_owner VARCHAR(128),
        lease_expires_at DATETIME,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        started_at DATETIME,
        completed_at DATETIME,
        deleted_at DATETIME,
        PRIMARY KEY (id),
        CONSTRAINT uq_palimpsest_exports_project_artifact UNIQUE (project_id, artifact_key)
    )
    """,
    """
    CREATE INDEX idx_palimpsest_exports_artifact ON palimpsest_image_exports (artifact_key)
    """,
    """
    CREATE INDEX idx_palimpsest_exports_claim ON palimpsest_image_exports (status, next_at)
    """,
    """
    CREATE INDEX idx_palimpsest_exports_digest ON palimpsest_image_exports (result_blob_digest)
    """,
    """
    CREATE INDEX idx_palimpsest_exports_project_created ON palimpsest_image_exports (project_id, deleted_at, created_at)
    """,
    """
    CREATE TABLE palimpsest_package_namespaces (
        project_id VARCHAR(64) NOT NULL,
        namespace VARCHAR(63) NOT NULL,
        project_name VARCHAR(255) NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (project_id),
        UNIQUE (namespace)
    )
    """,
    """
    CREATE TABLE palimpsest_worker_leases (
        name VARCHAR(255) NOT NULL,
        token VARCHAR(64) NOT NULL,
        expires_at DATETIME NOT NULL,
        PRIMARY KEY (name)
    )
    """,
    """
    CREATE TABLE palimpsest_hub_layer_access (
        blob_digest VARCHAR(71) NOT NULL,
        project_id VARCHAR(64) NOT NULL,
        created_by VARCHAR(128),
        created_at DATETIME NOT NULL,
        PRIMARY KEY (blob_digest, project_id),
        FOREIGN KEY(blob_digest) REFERENCES palimpsest_hub_layers (blob_digest) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE palimpsest_package_caches (
        id CHAR(32) NOT NULL,
        project_id VARCHAR(64) NOT NULL,
        package VARCHAR(255) NOT NULL,
        build_key CHAR(71) NOT NULL,
        cache_scope VARCHAR(48) NOT NULL,
        platform VARCHAR(64) NOT NULL,
        builder_fingerprint VARCHAR(128) NOT NULL,
        archive_digest CHAR(71) NOT NULL,
        archive_size_bytes BIGINT NOT NULL,
        created_at DATETIME NOT NULL,
        created_by VARCHAR(64) NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(project_id) REFERENCES palimpsest_package_namespaces (project_id)
    )
    """,
    """
    CREATE INDEX idx_package_cache_partition ON palimpsest_package_caches (project_id, package, cache_scope, platform, builder_fingerprint)
    """,
    """
    CREATE INDEX ix_palimpsest_package_caches_archive_digest ON palimpsest_package_caches (archive_digest)
    """,
    """
    CREATE TABLE palimpsest_package_keys (
        id CHAR(32) NOT NULL,
        project_id VARCHAR(64) NOT NULL,
        owner_user_id VARCHAR(64) NOT NULL,
        secret_hash CHAR(64) NOT NULL,
        name VARCHAR(128) NOT NULL,
        scope JSON NOT NULL,
        actions JSON NOT NULL,
        admin_issuer_context JSON,
        created_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        revoked_at DATETIME,
        PRIMARY KEY (id),
        FOREIGN KEY(project_id) REFERENCES palimpsest_package_namespaces (project_id)
    )
    """,
    """
    CREATE INDEX ix_palimpsest_package_keys_owner_user_id ON palimpsest_package_keys (owner_user_id)
    """,
    """
    CREATE TABLE palimpsest_packages (
        id CHAR(32) NOT NULL,
        project_id VARCHAR(64) NOT NULL,
        name VARCHAR(255) NOT NULL,
        package_type VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (project_id, name),
        FOREIGN KEY(project_id) REFERENCES palimpsest_package_namespaces (project_id)
    )
    """,
    """
    CREATE TABLE palimpsest_package_uploads (
        id CHAR(32) NOT NULL,
        project_id VARCHAR(64) NOT NULL,
        package VARCHAR(255) NOT NULL,
        key_id CHAR(32),
        owner_user_id VARCHAR(64) NOT NULL,
        resource VARCHAR(16) NOT NULL,
        request JSON NOT NULL,
        received_bytes BIGINT NOT NULL,
        status VARCHAR(16) NOT NULL,
        result JSON,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(project_id) REFERENCES palimpsest_package_namespaces (project_id),
        FOREIGN KEY(key_id) REFERENCES palimpsest_package_keys (id)
    )
    """,
    """
    CREATE INDEX ix_palimpsest_package_uploads_project_id ON palimpsest_package_uploads (project_id)
    """,
    """
    CREATE TABLE palimpsest_package_versions (
        package_id CHAR(32) NOT NULL,
        root_digest CHAR(71) NOT NULL,
        root_media_type VARCHAR(128) NOT NULL,
        graph JSON NOT NULL,
        platforms JSON NOT NULL,
        archive_digest CHAR(71) NOT NULL,
        archive_size_bytes BIGINT NOT NULL,
        total_bytes BIGINT NOT NULL,
        provenance JSON NOT NULL,
        pushed_by VARCHAR(64) NOT NULL,
        pushed_key_id CHAR(32),
        pushed_at DATETIME NOT NULL,
        PRIMARY KEY (package_id, root_digest),
        FOREIGN KEY(package_id) REFERENCES palimpsest_packages (id),
        FOREIGN KEY(pushed_key_id) REFERENCES palimpsest_package_keys (id)
    )
    """,
    """
    CREATE TABLE palimpsest_package_blob_references (
        package_id CHAR(32) NOT NULL,
        root_digest CHAR(71) NOT NULL,
        blob_digest CHAR(71) NOT NULL,
        PRIMARY KEY (package_id, root_digest, blob_digest),
        FOREIGN KEY(package_id, root_digest) REFERENCES palimpsest_package_versions (package_id, root_digest)
    )
    """,
    """
    CREATE INDEX ix_palimpsest_package_blob_references_blob_digest ON palimpsest_package_blob_references (blob_digest)
    """,
    """
    CREATE TABLE palimpsest_package_tags (
        package_id CHAR(32) NOT NULL,
        tag VARCHAR(128) NOT NULL,
        root_digest CHAR(71) NOT NULL,
        updated_at DATETIME NOT NULL,
        updated_by VARCHAR(64) NOT NULL,
        revision BIGINT NOT NULL,
        PRIMARY KEY (package_id, tag),
        FOREIGN KEY(package_id, root_digest) REFERENCES palimpsest_package_versions (package_id, root_digest)
    )
    """,
)


async def _create_release_0310_schema(connection):
    for statement in _RELEASE_0310_SQL:
        await connection.execute(text(statement))


@pytest.mark.asyncio
async def test_released_0310_full_fifteen_table_schema_bootstraps_then_orm_claims(tmp_path, monkeypatch):
    from sqlalchemy import inspect, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub import database
    from palimpsest_hub.image_models import PalimpsestHubImageJob
    from palimpsest_hub.services import image_jobs
    from palimpsest_hub.services.digest import compute_config_digest

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'released-0310.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(image_jobs, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(
        image_jobs,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_registry_hosts=("docker.io",),
            palimpsest_hub_build_timeout_seconds=60,
            palimpsest_hub_max_blob_bytes=4096,
        ),
    )
    try:
        async with engine.begin() as connection:
            await _create_release_0310_schema(connection)
            names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
            assert len(names) == 15
            assert "palimpsest_hub_image_jobs" not in names
            await connection.execute(
                text("""
                INSERT INTO palimpsest_hub_layers (
                    id, blob_digest, size_bytes, media_type, config_digest, name, kind,
                    config_json, project_id, is_published, created_at
                ) VALUES (1, :digest, 123, 'test/squashfs', :digest, 'legacy-layer',
                    'squashfs', '{}', 'original-project', 0, '2026-11-02 01:02:03.123456')
            """),
                {"digest": "sha256:" + "1" * 64},
            )
        await database.create_schema()
        await database.create_schema()
        async with factory() as session:
            assert (await session.scalars(select(PalimpsestHubImageJob))).all() == []
            request = {
                "reference": "docker.io/library/alpine:3",
                "platform": "linux/amd64",
                "os_family": "alpine",
                "os_version": "3",
                "visibility": "private",
            }
            session.add(
                PalimpsestHubImageJob(
                    id="00000000-0000-4000-8000-000000000001",
                    project_id="original-project",
                    created_by="original-user",
                    request_id="00000000-0000-4000-8000-000000000002",
                    kind="registry",
                    name="new-job",
                    request_digest=compute_config_digest({"kind": "registry", **request}),
                    request_json=request,
                )
            )
            await session.commit()
        claimed = await image_jobs.claim("00000000-0000-4000-8000-000000000003", 1)
        assert claimed["id"] == "00000000-0000-4000-8000-000000000001"
        async with factory() as session:
            job = (await session.scalars(select(PalimpsestHubImageJob))).one()
            assert job.visibility == "private" and job.status == "building"
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layers")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_published_images")) == 0
    finally:
        await engine.dispose()


async def _create_prepublication_image_schema(
    connection, *, creator="original-user", publication_ready=False, missing_fact=None
):
    """Full released base plus literal post-release HEAD image-table extensions."""
    import json

    from palimpsest_hub.services.digest import compute_config_digest

    await _create_release_0310_schema(connection)

    await connection.execute(
        text("""
        CREATE TABLE palimpsest_hub_build_contexts (
            project_id VARCHAR(64) NOT NULL, digest VARCHAR(71) NOT NULL,
            size_bytes BIGINT NOT NULL, created_by VARCHAR(128), created_at DATETIME NOT NULL,
            PRIMARY KEY (project_id, digest)
        )
    """)
    )
    await connection.execute(
        text("""
        CREATE TABLE palimpsest_hub_image_jobs (
            id CHAR(36) NOT NULL PRIMARY KEY, project_id VARCHAR(64) NOT NULL,
            created_by VARCHAR(128), request_id CHAR(36) NOT NULL, kind VARCHAR(16) NOT NULL,
            name VARCHAR(64) NOT NULL, request_digest VARCHAR(71) NOT NULL, request_json JSON NOT NULL,
            context_digest VARCHAR(71), status VARCHAR(16) NOT NULL, staged_outputs JSON NOT NULL,
            output_digests JSON NOT NULL, result_json JSON, error_code VARCHAR(32),
            lease_owner VARCHAR(128), lease_expires_at DATETIME, created_at DATETIME NOT NULL,
            started_at DATETIME, completed_at DATETIME,
            CONSTRAINT uq_image_job_project_request UNIQUE (project_id, request_id)
        )
    """)
    )
    await connection.execute(
        text("CREATE INDEX idx_image_jobs_project_created ON palimpsest_hub_image_jobs (project_id, created_at)")
    )
    await connection.execute(
        text("""
        INSERT INTO palimpsest_hub_build_contexts
        (project_id, digest, size_bytes, created_by, created_at)
        VALUES ('original-project', :digest, 123, :creator, '2026-11-02 01:02:03.123456')
    """),
        {"digest": "sha256:" + "a" * 64, "creator": creator},
    )
    request = {"reference": "docker.io/library/alpine:3", "platform": "linux/amd64"}
    if publication_ready and missing_fact != "os":
        # Explicit fixture facts, not a migration's inferred OS or invented owner.
        request.update(os_family="alpine", os_version="3")
    await connection.execute(
        text("""
        INSERT INTO palimpsest_hub_image_jobs
        (id, project_id, created_by, request_id, kind, name, request_digest, request_json,
         status, staged_outputs, output_digests, created_at)
        VALUES ('00000000-0000-4000-8000-000000000001', 'original-project', :job_creator,
         '00000000-0000-4000-8000-000000000002', 'registry', 'legacy-job', :digest,
         :request, 'queued', '[]', '[]', '2026-11-02 01:02:03.123456')
    """),
        {
            "digest": compute_config_digest({"kind": "registry", **request}),
            "request": json.dumps(request),
            "job_creator": creator if publication_ready and missing_fact != "creator" else None,
        },
    )


@pytest.mark.asyncio
async def test_prepublication_image_schema_upgrade_is_idempotent_and_real_orm_claim_works(tmp_path, monkeypatch):
    from sqlalchemy import inspect, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub import database
    from palimpsest_hub.image_models import PalimpsestHubBuildContext, PalimpsestHubImageJob
    from palimpsest_hub.services import image_jobs

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / '0310.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(image_jobs, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(
        image_jobs,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_registry_hosts=("docker.io",),
            palimpsest_hub_build_timeout_seconds=60,
            palimpsest_hub_max_blob_bytes=4096,
        ),
    )
    try:
        async with engine.begin() as connection:
            await _create_prepublication_image_schema(connection, publication_ready=True)
        await database.create_schema()
        await database.create_schema()
        async with engine.connect() as connection:
            primary = await connection.run_sync(
                lambda sync: inspect(sync).get_pk_constraint("palimpsest_hub_build_contexts")["constrained_columns"]
            )
            assert primary == ["project_id", "digest", "created_by"]
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("palimpsest_hub_image_jobs"))
            assert next(column for column in columns if column["name"] == "visibility")["nullable"] is False
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_published_images")) == 0
        async with factory() as session:
            context = (await session.scalars(select(PalimpsestHubBuildContext))).one()
            job = (await session.scalars(select(PalimpsestHubImageJob))).one()
            assert context.created_by == "original-user" and context.size_bytes == 123
            assert context.created_at.microsecond == 123456
            assert job.visibility == "internal" and job.created_by == "original-user"
            assert "visibility" not in job.request_json  # Preserve original digest/request bytes.
            session.add(
                PalimpsestHubBuildContext(
                    project_id=context.project_id, digest=context.digest, size_bytes=123, created_by="second-user"
                )
            )
            await session.commit()
        claimed = await image_jobs.claim("00000000-0000-4000-8000-000000000003", 1)
        assert claimed["id"] == "00000000-0000-4000-8000-000000000001"
        assert (await image_jobs.claim("00000000-0000-4000-8000-000000000003", 1)) == claimed
        await database.create_schema()
        async with factory() as session:
            job = (await session.scalars(select(PalimpsestHubImageJob))).one()
            assert job.status == "building" and job.visibility == "internal"
            assert job.lease_owner == claimed["lease"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_fact", [None, "creator", "os"], ids=["supported", "missing-creator", "missing-os"])
@pytest.mark.parametrize("inflight", [False, True], ids=["queued", "already-building"])
async def test_upgraded_image_worker_http_complete_preserves_publication_authority(
    tmp_path, monkeypatch, missing_fact, inflight
):
    import httpx
    from fastapi import FastAPI
    from palimpsest_local.image_manifest import manifest_from_result
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub import database
    from palimpsest_hub.api import image_jobs as image_api
    from palimpsest_hub.api.builds import require_build_lease
    from palimpsest_hub.image_models import PalimpsestHubImageJob, PalimpsestHubPublishedImage
    from palimpsest_hub.services import image_jobs, published_images
    from palimpsest_hub.services.hub_store import LocalPathBlobStore

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-complete.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    settings = SimpleNamespace(
        palimpsest_hub_registry_hosts=("docker.io",),
        palimpsest_hub_build_timeout_seconds=60,
        palimpsest_hub_max_blob_bytes=4096,
    )
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(image_jobs, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(image_jobs, "_store_or_503", lambda: store)
    for module in (image_jobs, image_api, published_images):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(image_api.router, prefix="/v1")
    try:
        async with engine.begin() as connection:
            await _create_prepublication_image_schema(connection, publication_ready=True, missing_fact=missing_fact)
            if inflight:
                await connection.execute(
                    text("""
                    UPDATE palimpsest_hub_image_jobs SET status='building', lease_owner=:lease,
                    started_at=:started, lease_expires_at=:expires
                """),
                    {
                        "lease": "00000000-0000-4000-8000-000000000004",
                        "started": datetime.now(UTC),
                        "expires": datetime.now(UTC) + timedelta(minutes=5),
                    },
                )
        await database.create_schema()
        await database.create_schema()
        async with factory() as session:
            original = (await session.scalars(select(PalimpsestHubImageJob))).one()
            original_request = dict(original.request_json)
        claimed = (
            {"id": original.id, "lease": original.lease_owner}
            if inflight
            else await image_jobs.claim("00000000-0000-4000-8000-000000000003", 1)
        )
        if missing_fact and not inflight:
            assert claimed is None
            async with factory() as session:
                job = (await session.scalars(select(PalimpsestHubImageJob))).one()
                assert job.status == "error" and job.error_code == "publication_metadata_required"
                assert job.visibility == "internal"
                assert (job.created_by is None) == (missing_fact == "creator")
                assert job.lease_owner is None and job.started_at is None
                assert job.request_json == original_request
                assert (await session.scalars(select(PalimpsestHubPublishedImage))).all() == []
            return  # No worker execution, byte upload or publication is authorized.
        app.dependency_overrides[require_build_lease] = lambda: claimed["lease"]
        payload = b"hsqs-synthetic-legacy-output"
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        descriptor = {
            "layers": [{"digest": digest, "size_bytes": len(payload)}],
            "source": {key: "sha256:" + "2" * 64 for key in ("root_digest", "manifest_digest", "config_digest")},
            "runtime": {"entrypoint": ["/bin/sh"], "cmd": [], "env": [], "working_dir": "/", "user": "0"},
        }
        descriptor["manifest"] = manifest_from_result(
            descriptor, os_family="alpine", os_version="3", source_kind="registry"
        )
        path = "/v1/image-worker/jobs/" + claimed["id"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://legacy.test") as client:
            if not missing_fact:
                staged = await client.put(path + "/outputs/" + digest, content=payload)
                assert staged.status_code == 200, staged.text
            response = await client.post(path + "/complete", json=descriptor)
            if missing_fact:
                assert response.status_code == 409, response.text
                assert response.json()["detail"] == "image publication requires proven creator and OS declarations"
                async with factory() as session:
                    job = (await session.scalars(select(PalimpsestHubImageJob))).one()
                    assert job.request_json == original_request
                    assert (await session.scalars(select(PalimpsestHubPublishedImage))).all() == []
                return
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "complete"
            assert response.json()["visibility"] == "internal"
            assert response.json()["published_image_id"] == claimed["id"]
            replay = await client.post(path + "/complete", json=descriptor)
            assert replay.status_code == 200, replay.text
        async with factory() as session:
            images = (await session.scalars(select(PalimpsestHubPublishedImage))).all()
            assert len(images) == 1
            assert images[0].created_by == "original-user" and images[0].visibility == "internal"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", [None, "", "   "])
async def test_legacy_unknown_context_creator_blocks_all_bootstrap_ddl(tmp_path, monkeypatch, creator):
    from sqlalchemy import inspect

    from palimpsest_hub import database

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unknown.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    try:
        async with engine.begin() as connection:
            await _create_prepublication_image_schema(connection, creator=creator)
            before = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
        with pytest.raises(RuntimeError, match="explicitly quarantine"):
            await database.create_schema()
        async with engine.connect() as connection:
            assert set(await connection.run_sync(lambda sync: inspect(sync).get_table_names())) == before
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("palimpsest_hub_image_jobs"))
            assert "visibility" not in {column["name"] for column in columns}
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_build_contexts")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_schema_failure_rolls_back_context_rebuild_and_visibility(tmp_path, monkeypatch):
    from sqlalchemy import inspect

    from palimpsest_hub import database
    from palimpsest_hub.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    try:
        async with engine.begin() as connection:
            await _create_prepublication_image_schema(connection)

        def fail_after_upgrade(*args, **kwargs):
            raise RuntimeError("synthetic create-all failure after actual SQLite upgrade DDL")

        monkeypatch.setattr(Base.metadata, "create_all", fail_after_upgrade)
        with pytest.raises(RuntimeError, match="synthetic create-all failure"):
            await database.create_schema()
        async with engine.connect() as connection:
            primary = await connection.run_sync(
                lambda sync: inspect(sync).get_pk_constraint("palimpsest_hub_build_contexts")["constrained_columns"]
            )
            assert primary == ["project_id", "digest"]
            columns = await connection.run_sync(lambda sync: inspect(sync).get_columns("palimpsest_hub_image_jobs"))
            assert "visibility" not in {column["name"] for column in columns}
            assert (
                await connection.scalar(text("SELECT created_by FROM palimpsest_hub_build_contexts")) == "original-user"
            )
            assert "__palimpsest_upgrade_build_contexts" not in await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incompatible_legacy_fk_parent_blocks_before_any_upgrade(tmp_path, monkeypatch):
    from sqlalchemy import inspect

    from palimpsest_hub import database

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bad-parent.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    try:
        async with engine.begin() as connection:
            await _create_prepublication_image_schema(connection)
            await connection.execute(text("DROP TABLE palimpsest_hub_layers"))
            await connection.execute(
                text("CREATE TABLE palimpsest_hub_layers (id INTEGER PRIMARY KEY, blob_digest VARCHAR(71))")
            )
        with pytest.raises(RuntimeError, match="incompatible legacy key"):
            await database.create_schema()
        async with engine.connect() as connection:
            primary = await connection.run_sync(
                lambda sync: inspect(sync).get_pk_constraint("palimpsest_hub_build_contexts")["constrained_columns"]
            )
            assert primary == ["project_id", "digest"]
            assert "palimpsest_hub_layer_nodes" not in await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
    finally:
        await engine.dispose()


def _ownership_process(url, root, lock_path, mode, channel, gate):
    """Real API SQL/process consumer; recipes/provider deletes remain synthetic."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub.services import builds
    from palimpsest_hub.services.hub_store import LocalPathBlobStore
    from palimpsest_hub.services.worker_leases import role_lease

    async def consume():
        engine = create_async_engine(url, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        # Every build API gets distinct CAS/flock paths: SQL must own single-flight.
        private_api = mode in ("claim", "build") or mode.startswith("status:")
        store = LocalPathBlobStore(Path(root) / (f"api-{os.getpid()}" if private_api else "store"))
        base = b"raw ownership input"
        base_digest = "sha256:" + hashlib.sha256(base).hexdigest()
        store.blobs_dir.mkdir(parents=True, exist_ok=True)
        store.blob_path(base_digest).write_bytes(base)
        builds._factory_or_503 = lambda: factory
        builds._store_or_503 = lambda: store
        builds.get_settings = lambda: SimpleNamespace(
            palimpsest_hub_build_timeout_seconds=60,
            palimpsest_hub_max_blob_bytes=1024 * 1024,
        )

        owner = f"smoke-{os.getpid()}"
        try:
            if gate is not None:
                await asyncio.to_thread(gate.wait)
            if mode == "claim":
                claim = await builds.claim_build(str(uuid4()), 1)
                channel.send({"claim": claim["id"] if claim else None, "lease": claim["lease"] if claim else None})
            elif mode == "build":
                from starlette.requests import Request

                claim = await builds.claim_build(str(uuid4()), 1)
                assert claim is not None
                tree = Path(root) / "private-worker" / claim["id"]
                tree.mkdir(parents=True)
                payload = b"hsqs\x00\x00ownership-proof"
                (tree / "result.sqsh").write_bytes(payload)
                with (Path(root) / "accepted-mutations").open("ab", buffering=0) as log:
                    log.write(b"accepted\n")
                channel.send({"event": "accepted", "id": claim["id"], "lease": claim["lease"]})
                assert await asyncio.to_thread(channel.recv) == "finish"

                async def receive():
                    return {"type": "http.request", "body": payload, "more_body": False}

                request = Request({"type": "http", "method": "PUT", "path": "/output", "headers": []}, receive)
                result = await builds.upload_output(
                    claim["id"], claim["lease"], request, "sha256:" + hashlib.sha256(payload).hexdigest(), len(payload)
                )
                channel.send({"status": result.status, "cas": str(store.blob_path(result.output_digest))})
            elif mode.startswith("status:"):
                _, job_id, lease = mode.split(":")
                result = await builds.worker_status(job_id, lease)
                channel.send({"status": result.status, "lease": result.lease_owner})
            elif mode == "export-claim":
                from palimpsest_hub.services import image_exports

                image_exports.get_session_factory = lambda: factory
                export = await image_exports.claim_next_image_export(owner=owner)
                channel.send({"claim": export.id if export else None})
            elif mode == "maintenance":
                from palimpsest_hub.services import image_exports

                image_exports.get_session_factory = lambda: factory
                image_exports.get_blob_store = lambda: store
                await image_exports.run_export_maintenance(max_age_seconds=1)
                channel.send({"maintained": True})
            elif mode == "delegation":
                from palimpsest_hub.services import image_exports

                image_exports.get_session_factory = lambda: factory
                image_exports._ROLE_LEASE_SECONDS = 3
                image_exports._CLEANUP_LEASE = timedelta(seconds=3)

                def delete_trust(_delegation):
                    with (Path(root) / "accepted-provider-deletes").open("ab", buffering=0) as log:
                        log.write(b"deleted\n")
                    channel.send({"event": "provider-delete"})
                    assert channel.recv() == "finish"
                    return True

                image_exports.delete_export_trust = delete_trust
                finished = await image_exports.run_delegation_cleanup()
                channel.send({"finished": finished})
            elif mode.startswith("role:"):
                _, name, action = mode.split(":")
                async with role_lease(factory, name, ttl_seconds=3) as lease:
                    channel.send({"token": lease.token if lease else None})
                    if lease is not None and action == "hold":
                        assert await asyncio.to_thread(channel.recv) == "finish"
            else:
                raise AssertionError("unknown ownership smoke mode")
        finally:
            await engine.dispose()

    try:
        # Independent flock paths are intentional: SQL, not this fd, is shared authority.
        with Path(lock_path).open("wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(consume())
    except BaseException as exc:
        # Never print SQL URLs, provider data or raw connection exceptions.
        with suppress(BrokenPipeError, EOFError):
            channel.send({"error": type(exc).__name__})
    finally:
        channel.close()


async def _ownership_receive(channel):
    ready = await asyncio.to_thread(channel.poll, 30)
    assert ready, "ownership child did not report before deadline"
    result = channel.recv()
    assert "error" not in result, f"ownership child failed: {result.get('error')}"
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_count", [2, 3])
async def test_shared_sql_process_ownership_smoke(tmp_path, worker_count):
    """Opt-in real MariaDB/process/flock proof; NOT KVM or live provider proof."""
    from sqlalchemy import inspect, select, update
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub.models import (
        Base,
        PalimpsestHubBuild,
        PalimpsestHubLayer,
        PalimpsestImageExport,
        PalimpsestImageExportDelegation,
        PalimpsestWorkerLease,
    )
    from palimpsest_hub.services.digest import compute_config_digest
    from palimpsest_hub.services.hub_store import LocalPathBlobStore
    from palimpsest_hub.services.worker_leases import RoleLease, role_lease

    url = os.environ.get("PALIMPSEST_OWNERSHIP_SQL_URL")
    if not url:
        pytest.skip("set PALIMPSEST_OWNERSHIP_SQL_URL to an empty disposable loopback MariaDB database")
    parsed = make_url(url)
    assert parsed.drivername == "mysql+asyncmy", "ownership smoke requires MariaDB/asyncmy"
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}, "ownership SQL must be local"
    assert re.fullmatch(r"palimpsest_ownership_[A-Za-z0-9_]+", parsed.database or ""), "unsafe database name"
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    context = multiprocessing.get_context("spawn")
    children = []
    owns_schema = False
    base_bytes = b"synthetic ownership-proof base image"
    base_digest = "sha256:" + hashlib.sha256(base_bytes).hexdigest()

    def start(mode, gate=None):
        parent, child = context.Pipe()
        process = context.Process(
            target=_ownership_process,
            args=(url, str(tmp_path), str(tmp_path / f"worker-{len(children)}.lock"), mode, child, gate),
        )
        process.start()
        child.close()
        children.append((process, parent))
        return process, parent

    async def joined(process):
        await asyncio.to_thread(process.join, 30)
        assert not process.is_alive(), "ownership child did not exit"
        assert process.exitcode == 0, "ownership child exited abnormally"

    async def enqueue():
        job_id = str(uuid4())
        base_digest = "sha256:" + hashlib.sha256(b"raw ownership input").hexdigest()
        recipe = f"FROM {base_digest}\nRUN synthetic"
        async with factory() as session, session.begin():
            session.add(
                PalimpsestHubBuild(
                    id=job_id,
                    project_id="smoke-project",
                    created_by="smoke-user",
                    name="ownership",
                    recipe=recipe,
                    recipe_digest="sha256:" + hashlib.sha256(recipe.encode()).hexdigest(),
                    base_digest=base_digest,
                    layer_digests=[],
                    status="queued",
                    created_at=datetime.now(UTC),
                )
            )
        return job_id

    async def expire_build(job_id):
        async with factory() as session, session.begin():
            await session.execute(
                update(PalimpsestHubBuild)
                .where(PalimpsestHubBuild.id == job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

    try:
        async with engine.begin() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            assert not names, "ownership smoke refuses a nonempty database"
            assert "mariadb" in (await connection.scalar(text("SELECT VERSION()"))).lower()
            assert (await connection.scalar(text("SELECT @@default_storage_engine"))).lower() == "innodb"
            await connection.run_sync(Base.metadata.create_all)
            owns_schema = True
        store = LocalPathBlobStore(tmp_path / "store")
        base_path = store.blob_path(base_digest)
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_bytes(base_bytes)
        base_config = {"name": "ownership-base", "kind": "cloud-image", "arch": "x86_64", "disk_format": "raw"}
        async with factory() as session, session.begin():
            session.add(
                PalimpsestHubLayer(
                    blob_digest=base_digest,
                    size_bytes=len(base_bytes),
                    media_type="application/octet-stream",
                    config_digest=compute_config_digest(base_config),
                    config_json=base_config,
                    name="ownership-base",
                    kind="cloud-image",
                    arch="x86_64",
                    disk_format="raw",
                    project_id="smoke-project",
                    created_by="smoke-user",
                    is_published=False,
                )
            )

        base = b"raw ownership input"
        base_digest = "sha256:" + hashlib.sha256(base).hexdigest()
        async with factory() as session, session.begin():
            session.add(
                PalimpsestHubLayer(
                    blob_digest=base_digest,
                    size_bytes=len(base),
                    media_type="application/octet-stream",
                    disk_format="raw",
                    arch="x86_64",
                    config_digest=base_digest,
                    name="ownership-base",
                    kind="cloud-image",
                    config_json={},
                    project_id="smoke-project",
                    is_published=False,
                )
            )

        # Race API claims with genuinely disjoint CAS and process flock paths.
        raced_job = await enqueue()
        gate = context.Event()
        racers = [start("claim", gate) for _ in range(worker_count)]
        gate.set()
        claims = await asyncio.gather(*(_ownership_receive(pipe) for _, pipe in racers))
        assert [item["claim"] for item in claims].count(raced_job) == 1
        assert sum(item["claim"] is None for item in claims) == worker_count - 1
        receipt = next(item for item in claims if item["claim"] is not None)
        for process, _ in racers:
            await joined(process)
        await expire_build(raced_job)
        observer, pipe = start(f"status:{raced_job}:{receipt['lease']}")
        assert await _ownership_receive(pipe) == {"status": "error", "lease": receipt["lease"]}
        await joined(observer)

        # Live execution is private; API peers cannot reclaim/replay it.
        live_job = await enqueue()
        builder, pipe = start("build")
        accepted = await _ownership_receive(pipe)
        assert accepted["event"] == "accepted" and accepted["id"] == live_job
        tree = tmp_path / "private-worker" / live_job
        probes = [start("claim") for _ in range(worker_count - 1)]
        for process, probe_pipe in probes:
            assert await _ownership_receive(probe_pipe) == {"claim": None, "lease": None}
            await joined(process)
        async with factory() as session:
            job = await session.get(PalimpsestHubBuild, live_job)
            assert job.status == "building" and job.lease_owner == accepted["lease"]
            deadline = job.lease_expires_at
        assert (tree / "result.sqsh").is_file()
        pipe.send("finish")
        completed = await _ownership_receive(pipe)
        assert completed["status"] == "complete" and Path(completed["cas"]).is_file()
        await joined(builder)
        async with factory() as session:
            job = await session.get(PalimpsestHubBuild, live_job)
            layer = await session.scalar(
                select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == job.output_digest)
            )
            assert job.lease_owner == accepted["lease"] and job.lease_expires_at == deadline
            assert layer.project_id == "smoke-project" and layer.created_by == "smoke-user" and not layer.is_published
        assert tree.exists(), "API must not clean a remote worker's private tree"

        # A crashed worker expires terminally with its original receipt nonce.
        crashed_job = await enqueue()
        builder, pipe = start("build")
        crashed = await _ownership_receive(pipe)
        assert crashed["event"] == "accepted"
        builder.kill()
        await asyncio.to_thread(builder.join, 30)
        assert not builder.is_alive()
        await expire_build(crashed_job)
        observer, pipe = start(f"status:{crashed_job}:{crashed['lease']}")
        assert await _ownership_receive(pipe) == {"status": "error", "lease": crashed["lease"]}
        await joined(observer)
        observer, pipe = start("claim")
        assert await _ownership_receive(pipe) == {"claim": None, "lease": None}
        await joined(observer)
        assert (tmp_path / "private-worker" / crashed_job / "result.sqsh").is_file()
        assert (tmp_path / "accepted-mutations").read_bytes() == b"accepted\naccepted\n"

        export_id = str(uuid4())
        async with factory() as session, session.begin():
            session.add(
                PalimpsestImageExport(
                    id=export_id,
                    project_id="smoke-project",
                    created_by="smoke-user",
                    source_image_id="synthetic-glance",
                    source_name="ownership",
                    source_disk_format="raw",
                    source_size_bytes=1,
                    source_fingerprint="1" * 64,
                    artifact_key="2" * 64,
                    target_disk_format="qcow2",
                    status="queued",
                    next_at=datetime.now(UTC),
                )
            )
        gate = context.Event()
        exporters = [start("export-claim", gate) for _ in range(worker_count)]
        gate.set()
        claims = await asyncio.gather(*(_ownership_receive(pipe) for _, pipe in exporters))
        assert [item["claim"] for item in claims].count(export_id) == 1
        assert sum(item["claim"] is None for item in claims) == worker_count - 1
        for process, _ in exporters:
            await joined(process)

        async with factory() as session:
            export = await session.get(PalimpsestImageExport, export_id)
            owner_key = hashlib.sha256(export.lease_owner.encode("utf-8")).hexdigest()[:12]
            scratch = tmp_path / "store" / "exports" / f"{export_id}-{export.attempts}-{owner_key}"
        scratch.mkdir(parents=True)
        (scratch / "live-output").write_bytes(b"live staged output")
        os.utime(scratch, (1, 1))
        maintainers = [start("maintenance") for _ in range(worker_count)]
        for process, maintenance_pipe in maintainers:
            assert await _ownership_receive(maintenance_pipe) == {"maintained": True}
            await joined(process)
        assert (scratch / "live-output").read_bytes() == b"live staged output"
        async with factory() as session, session.begin():
            await session.execute(
                update(PalimpsestImageExport)
                .where(PalimpsestImageExport.id == export_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        maintainer, maintenance_pipe = start("maintenance")
        assert await _ownership_receive(maintenance_pipe) == {"maintained": True}
        await joined(maintainer)
        assert not scratch.exists()

        # Exercise the actual delegation consumer with a synthetic provider delete
        # held beyond its nominal TTL; SQL fences must exclude a second effect.
        async with factory() as session, session.begin():
            now = datetime.now(UTC)
            session.add(
                PalimpsestImageExportDelegation(
                    trust_id="ownership-trust",
                    project_id="smoke-project",
                    trustor_user_id="smoke-user",
                    trustee_user_id="smoke-service",
                    role_names=["member"],
                    expires_at=now + timedelta(hours=1),
                    state="cleanup",
                    cleanup_attempts=0,
                    cleanup_next_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        deleter, pipe = start("delegation")
        assert (await _ownership_receive(pipe))["event"] == "provider-delete"
        peers = [start("delegation") for _ in range(worker_count - 1)]
        await asyncio.sleep(4)  # Deliberately longer than the synthetic 3-second lease.
        assert (tmp_path / "accepted-provider-deletes").read_bytes() == b"deleted\n"
        pipe.send("finish")
        assert (await _ownership_receive(pipe))["finished"] == 1
        await joined(deleter)
        for process, peer_pipe in peers:
            assert (await _ownership_receive(peer_pipe))["finished"] == 0
            await joined(process)
        async with factory() as session:
            delegation = await session.get(PalimpsestImageExportDelegation, "ownership-trust")
            assert delegation.state == "deleted"
        assert (tmp_path / "accepted-provider-deletes").read_bytes() == b"deleted\n"

        for name in ("export-maintenance", "export-delegation-cleanup"):
            holder, pipe = start(f"role:{name}:hold")
            old_token = (await _ownership_receive(pipe))["token"]
            assert old_token
            peers = [start(f"role:{name}:try") for _ in range(worker_count - 1)]
            for process, peer_pipe in peers:
                assert (await _ownership_receive(peer_pipe))["token"] is None
                await joined(process)
            holder.kill()
            await asyncio.to_thread(holder.join, 30)
            assert not holder.is_alive()
            async with factory() as session, session.begin():
                await session.execute(
                    update(PalimpsestWorkerLease)
                    .where(PalimpsestWorkerLease.name == name)
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            async with role_lease(factory, name, ttl_seconds=30) as successor:
                assert successor is not None and successor.token != old_token
                await RoleLease(factory, name, old_token, 30).release()
                async with factory() as session:
                    held = await session.scalar(select(PalimpsestWorkerLease).where(PalimpsestWorkerLease.name == name))
                    assert held.token == successor.token
        print(
            f"ownership proof: {worker_count} real SQL/process workers, independent flocks; "
            "build API single-flight and fixed deadlines, remote execution/provider synthetic; NOT production proof"
        )
    finally:
        for process, channel in children:
            if process.is_alive():
                process.kill()
            await asyncio.to_thread(process.join, 30)
            channel.close()
        if owns_schema and all(not process.is_alive() for process, _ in children):
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_adds_missing_execution_tables_without_changing_existing_rows(tmp_path, monkeypatch):
    from sqlalchemy import inspect, select

    from palimpsest_hub import database, image_models, instance_models, native_models  # noqa: F401
    from palimpsest_hub.models import Base, PalimpsestHubBuild, RuntimeBlobReference

    new_tables = {
        image_models.PalimpsestHubBuildContext.__tablename__,
        image_models.PalimpsestHubImageJob.__tablename__,
        instance_models.PalimpsestHubInstance.__tablename__,
        RuntimeBlobReference.__tablename__,
    }
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-execution.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    builds = PalimpsestHubBuild.__table__
    now = datetime(2026, 10, 30, 1, 2, 3, 456789, tzinfo=UTC)
    try:
        async with engine.begin() as connection:
            legacy = [table for table in Base.metadata.sorted_tables if table.name not in new_tables]
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=legacy))
            await connection.execute(
                builds.insert().values(
                    id="00000000-0000-4000-8000-000000000001",
                    project_id="original-project",
                    created_by="original-user",
                    name="retained-build",
                    recipe="RUN true",
                    recipe_digest="sha256:" + "1" * 64,
                    base_digest="sha256:" + "2" * 64,
                    layer_digests=[],
                    status="error",
                    error_code="worker_interrupted",
                    lease_expires_at=now,
                    created_at=now,
                )
            )

        async def snapshot():
            async with engine.connect() as connection:
                objects = list(
                    (await connection.execute(text("SELECT type, name, tbl_name, sql FROM sqlite_master"))).all()
                )
                row = (await connection.execute(select(builds))).mappings().one()
                names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
                return {tuple(item) for item in objects}, dict(row), names

        before_schema, before_row, before_tables = await snapshot()
        assert not (before_tables & new_tables)
        await database.create_schema()
        after_schema, after_row, after_tables = await snapshot()
        assert after_tables - before_tables == new_tables
        assert before_schema <= after_schema
        assert after_row == before_row
        await database.create_schema()
        repeated_schema, repeated_row, repeated_tables = await snapshot()
        assert repeated_schema == after_schema
        assert repeated_row == before_row
        assert repeated_tables == after_tables
    finally:
        await engine.dispose()
