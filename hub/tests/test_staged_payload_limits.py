"""Staged-but-unregistered publication payloads: aggregate byte quota and expiry (GATE-01 P2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import test_public_images
from sqlalchemy import func, select, update
from sqlalchemy.dialects.mysql import VARCHAR
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_public_images import ALICE, AMY, BOB, PROJECT_A, PROJECT_B, digest, human, publish, stage

from palimpsest_hub.image_models import (
    PalimpsestHubImageMembership,
    PalimpsestHubImageSourceBlob,
    PalimpsestHubPublicationPayload,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import Base, RuntimeBlobReference
from palimpsest_hub.services import staged_payloads

hub_images = test_public_images.hub_images
ALICE_H = human(ALICE, PROJECT_A)
AMY_H = human(AMY, PROJECT_A)
BOB_H = human(BOB, PROJECT_B)


def _blob(tag: str, size: int = 400) -> bytes:
    return (tag.encode() * size)[:size]


async def _count(h, user: str) -> int:
    async with h.factory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(PalimpsestHubPublicationPayload)
            .where(PalimpsestHubPublicationPayload.created_by == user)
        )


async def _backdate(h, user: str, *, hours: int) -> None:
    moment = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
    async with h.factory() as session:
        await session.execute(
            update(PalimpsestHubPublicationPayload)
            .where(PalimpsestHubPublicationPayload.created_by == user)
            .values(created_at=moment)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_aggregate_quota_rejects_the_overflowing_blob_with_a_structured_code_and_is_per_user(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_staged_max_bytes = 1000
    first, second, third = _blob("a"), _blob("b"), _blob("c")
    assert (await stage(h, first, ALICE_H)).status_code == 200
    assert (await stage(h, second, ALICE_H)).status_code == 200
    refused = await stage(h, third, ALICE_H)
    assert refused.status_code == 413
    assert refused.json()["detail"] == {"code": "staged_payload_limit", "limit_bytes": 1000, "staged_bytes": 800}
    assert await _count(h, ALICE) == 2
    # Re-staging a payload the caller already holds consumes no new quota, even at the cap.
    assert (await stage(h, first, ALICE_H)).status_code == 200
    # Another user has an independent allowance.
    assert (await stage(h, third, AMY_H)).status_code == 200


@pytest.mark.asyncio
async def test_expired_unregistered_payloads_release_reference_and_quota_even_when_the_upload_is_refused(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_staged_max_bytes = 900
    h.settings.palimpsest_hub_staged_ttl_seconds = 3600
    old_a, old_b = _blob("a"), _blob("b")
    assert (await stage(h, old_a, ALICE_H)).status_code == 200
    assert (await stage(h, old_b, ALICE_H)).status_code == 200
    await _backdate(h, ALICE, hours=2)
    too_big = _blob("z", 901)
    assert (await stage(h, too_big, ALICE_H)).status_code == 413
    # The refusal did not roll the expiry back.
    assert await _count(h, ALICE) == 0
    async with h.factory() as session:
        references = (await session.scalars(select(RuntimeBlobReference.digest))).all()
    assert digest(old_a) not in references and digest(old_b) not in references
    assert (await stage(h, _blob("c"), ALICE_H)).status_code == 200
    # Expired rows no longer authorize publication of that payload.
    gone = await test_public_images.publish_raw(h, [old_a], ALICE_H)
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_registered_payloads_neither_count_nor_expire_and_restaging_them_is_free(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_staged_max_bytes = 1000
    h.settings.palimpsest_hub_staged_ttl_seconds = 3600
    kept = b"hsqs-" + _blob("k", 600)
    await publish(h, kept, ALICE_H, visibility="private", name="kept")
    await _backdate(h, ALICE, hours=48)
    # 600 registered bytes would exceed the 1000 byte cap if they counted.
    assert (await stage(h, _blob("x", 900), ALICE_H)).status_code == 200
    # Re-staging the registered base (as every push does) is free even though 900 bytes are staged.
    assert (await stage(h, kept, ALICE_H)).status_code == 200
    async with h.factory() as session:
        digests = set((await session.scalars(select(PalimpsestHubPublicationPayload.blob_digest))).all())
    assert digest(kept) in digests


@pytest.mark.asyncio
async def test_another_projects_publication_of_the_same_bytes_neither_frees_quota_nor_leaks_existence(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_staged_max_bytes = 1000
    shared = b"hsqs-" + _blob("s", 600)
    await publish(h, shared, BOB_H, visibility="private", name="bobs")
    assert (await stage(h, shared, ALICE_H)).status_code == 200
    # Alice's 605 staged bytes still count for Alice: foreign registration is not a registration for her.
    refused = await stage(h, _blob("y", 600), ALICE_H)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (413, "staged_payload_limit")
    assert refused.json()["detail"]["staged_bytes"] == len(shared)


@pytest.mark.asyncio
async def test_maintenance_sweep_expires_idle_rows_for_users_who_never_stage_again(hub_images):
    h = hub_images
    old, kept = _blob("o"), b"hsqs-" + _blob("k", 300)
    assert (await stage(h, old, ALICE_H)).status_code == 200
    await publish(h, kept, ALICE_H, visibility="private", name="kept")
    await _backdate(h, ALICE, hours=48)
    async with h.factory() as session:
        removed = await staged_payloads.expire_staged_payloads(session, ttl_seconds=3600)
        await session.commit()
    assert removed == 1
    async with h.factory() as session:
        remaining = set((await session.scalars(select(PalimpsestHubPublicationPayload.blob_digest))).all())
        references = set((await session.scalars(select(RuntimeBlobReference.digest))).all())
    assert digest(old) not in remaining and digest(old) not in references
    assert digest(kept) in remaining


@pytest.mark.asyncio
async def test_registration_is_by_the_stagers_own_image_not_by_whoever_registered_the_layer_first(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_staged_max_bytes = 1000
    h.settings.palimpsest_hub_staged_ttl_seconds = 3600
    shared = b"hsqs-" + _blob("s", 395)
    # Another user of the same project registers the layer first (the physical layer row records only that user).
    await publish(h, shared, AMY_H, visibility="private", name="amys")
    # A foreign project registers other bytes first too; neither may change Alice's accounting.
    other = b"hsqs-" + _blob("t", 395)
    await publish(h, other, BOB_H, visibility="private", name="bobs")
    for payload, name in ((shared, "alices"), (other, "alices-other")):
        assert (await stage(h, payload, ALICE_H)).status_code == 200
        before = await stage(h, _blob("q", 700), ALICE_H)
        assert before.status_code == 413
        assert before.json()["detail"]["staged_bytes"] == len(payload)
        response = await test_public_images.publish_raw(h, [payload], ALICE_H, name=name)
        assert response.status_code == 201, response.text
        await _backdate(h, ALICE, hours=48)
        # Registered by Alice's own image: no longer counted and never expired.
        assert (await stage(h, _blob("q", 700), ALICE_H)).status_code == 200
        async with h.factory() as session:
            await session.execute(
                PalimpsestHubPublicationPayload.__table__.delete().where(
                    PalimpsestHubPublicationPayload.blob_digest == digest(_blob("q", 700))
                )
            )
            await session.commit()
        async with h.factory() as session:
            kept = set((await session.scalars(select(PalimpsestHubPublicationPayload.blob_digest))).all())
        assert digest(payload) in kept


@pytest.mark.asyncio
async def test_restaging_refreshes_the_age_so_an_in_progress_push_is_not_expired(hub_images):
    h = hub_images
    h.settings.palimpsest_hub_staged_max_bytes = 10_000
    h.settings.palimpsest_hub_staged_ttl_seconds = 3600
    payload = _blob("r")
    assert (await stage(h, payload, ALICE_H)).status_code == 200
    await _backdate(h, ALICE, hours=2)
    assert (await stage(h, payload, ALICE_H)).status_code == 200
    # The next stage would have expired the old row; the refreshed one survives.
    assert (await stage(h, _blob("s"), ALICE_H)).status_code == 200
    async with h.factory() as session:
        kept = set((await session.scalars(select(PalimpsestHubPublicationPayload.blob_digest))).all())
    assert digest(payload) in kept


@pytest.mark.asyncio
@pytest.mark.parametrize("image_project,image_user", [("project", "User"), ("Project", "user")])
@pytest.mark.parametrize("source", [False, True])
async def test_registration_requires_byte_exact_principal_under_nocase(
    tmp_path, monkeypatch, image_project, image_user, source
):
    with monkeypatch.context() as schema:
        for model in (PalimpsestHubPublishedImage, PalimpsestHubPublicationPayload):
            for name, length in (("project_id", 64), ("created_by", 128)):
                schema.setattr(model.__table__.c[name], "type", VARCHAR(length, collation="NOCASE"))
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'staged-nocase.sqlite'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    foreign_digest, own_digest = digest(b"foreign"), digest(b"own")
    try:
        async with factory() as session:
            for image_id, project, user, blob in (
                ("foreign", image_project, image_user, foreign_digest),
                ("own", "Project", "User", own_digest),
            ):
                session.add(
                    PalimpsestHubPublishedImage(
                        id=image_id,
                        project_id=project,
                        created_by=user,
                        request_id=image_id,
                        name=image_id,
                        manifest_digest=blob,
                        manifest_json={},
                        leaf_id=blob,
                        storage_binding_json={},
                    )
                )
                session.add(
                    PalimpsestHubPublicationPayload(
                        project_id="Project", created_by="User", blob_digest=blob, size_bytes=400
                    )
                )
                if source:
                    session.add(
                        PalimpsestHubImageSourceBlob(
                            image_id=image_id,
                            role="manifest",
                            ordinal=0,
                            project_id=project,
                            blob_digest=blob,
                            size_bytes=400,
                        )
                    )
                else:
                    session.add(
                        PalimpsestHubImageMembership(
                            image_id=image_id, ordinal=0, project_id=project, node_id=blob, blob_digest=blob
                        )
                    )
            await session.commit()
        async with factory() as session:
            with pytest.raises(staged_payloads.StagedQuotaExceeded) as refused:
                await staged_payloads.enforce_staging_limits(
                    session, "Project", "User", digest(b"new"), 201, max_bytes=600, ttl_seconds=3600
                )
            assert refused.value.detail["staged_bytes"] == 400
            await session.execute(
                update(PalimpsestHubPublicationPayload).values(
                    created_at=(datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None)
                )
            )
            assert await staged_payloads.expire_staged_payloads(session, ttl_seconds=3600) == 1
            await session.commit()
            remaining = set((await session.scalars(select(PalimpsestHubPublicationPayload.blob_digest))).all())
            assert remaining == {own_digest}
    finally:
        await engine.dispose()
