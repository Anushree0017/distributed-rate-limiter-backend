"""Dumb data access for `clients` + `client_secrets`. No business rules here
(secret verification, the two-active-secrets cap, dummy-hash timing-oracle
defense, etc. live in `services/auth_service.py` / `services/client_service.py`)
— this layer just talks to the DB.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from model.client import Client
from model.client_secret import ClientSecret


class ClientRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    def add(self, client: Client) -> None:
        self._session.add(client)

    async def get_by_pk(self, pk: uuid.UUID) -> Client | None:
        return await self._session.get(Client, pk)

    async def get_by_client_id(self, client_id: str) -> Client | None:
        result = await self._session.execute(select(Client).where(Client.client_id == client_id))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Client]:
        """Every row, unpaginated — used by `services/clients_loader.py` to
        populate `ClientsCache` at startup and on each poll cycle.
        """
        result = await self._session.execute(select(Client))
        return list(result.scalars().all())

    async def list(self, page: int, page_size: int) -> tuple[list[Client], int]:
        total = (await self._session.execute(select(func.count()).select_from(Client))).scalar_one()
        stmt = select(Client).order_by(Client.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        result = await self._session.execute(stmt)
        return list(result.scalars().all()), total

    async def count_active_secrets(self, client_pk: uuid.UUID) -> int:
        now = datetime.now(timezone.utc)
        stmt = select(func.count()).select_from(ClientSecret).where(
            ClientSecret.client_pk == client_pk,
            ClientSecret.revoked_at.is_(None),
            (ClientSecret.expires_at.is_(None)) | (ClientSecret.expires_at > now),
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def list_active_secrets(self, client_pk: uuid.UUID) -> list[ClientSecret]:
        now = datetime.now(timezone.utc)
        stmt = select(ClientSecret).where(
            ClientSecret.client_pk == client_pk,
            ClientSecret.revoked_at.is_(None),
            (ClientSecret.expires_at.is_(None)) | (ClientSecret.expires_at > now),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_secrets(self, client_pk: uuid.UUID) -> list[ClientSecret]:
        result = await self._session.execute(
            select(ClientSecret).where(ClientSecret.client_pk == client_pk).order_by(ClientSecret.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_secret_by_id(self, secret_id: uuid.UUID) -> ClientSecret | None:
        return await self._session.get(ClientSecret, secret_id)

    def add_secret(self, secret: ClientSecret) -> None:
        self._session.add(secret)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def refresh(self, obj, attribute_names: list[str] | None = None) -> None:
        await self._session.refresh(obj, attribute_names=attribute_names)
