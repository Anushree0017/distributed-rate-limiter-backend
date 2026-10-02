"""Business rules for the clients admin API (Phase 6, Step 8): client CRUD,
secret issuance/rotation/revocation. Repositories stay dumb data access; this
is where the two-active-secrets cap, the "can't revoke the last active
secret of an active client" guard, etc. live.
"""
import uuid

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    ClientIdConflictError,
    ClientNotFoundError,
    ClientSecretNotFoundError,
    LastActiveSecretError,
    TooManyActiveSecretsError,
)
from core.security.secrets import generate_secret, hash_secret, secret_hint
from dto.client_dto import ClientCreateRequestDTO, ClientUpdateRequestDTO
from model.client import Client
from model.client_secret import ClientSecret
from repositories.client_repository import ClientRepository

_MAX_ACTIVE_SECRETS_PER_CLIENT = 2


class ClientService:
    def __init__(self, repository: ClientRepository):
        self._repository = repository

    async def create_client(self, data: ClientCreateRequestDTO) -> tuple[Client, str]:
        """Returns `(client, plaintext_secret)` — the client's first secret is
        minted at creation time (per the plan's "returns the plaintext secret
        once" for `POST /clients`), since a client with zero secrets could
        never authenticate at all.
        """
        existing = await self._repository.get_by_client_id(data.client_id)
        if existing is not None:
            raise ClientIdConflictError(data.client_id)

        client = Client(
            client_id=data.client_id,
            name=data.name,
            description=data.description,
            scopes=data.scopes,
        )
        self._repository.add(client)
        try:
            await self._repository.commit()
        except IntegrityError:
            await self._repository.rollback()
            raise ClientIdConflictError(data.client_id)
        await self._repository.refresh(client)

        plaintext_secret = generate_secret()
        self._repository.add_secret(
            ClientSecret(client_pk=client.id, secret_hash=hash_secret(plaintext_secret), secret_hint=secret_hint(plaintext_secret))
        )
        await self._repository.commit()
        return client, plaintext_secret

    async def get_client(self, client_id: str) -> Client:
        client = await self._repository.get_by_client_id(client_id)
        if client is None:
            raise ClientNotFoundError(client_id)
        return client

    async def list_clients(self, page: int, page_size: int) -> tuple[list[Client], int]:
        return await self._repository.list(page, page_size)

    async def update_client(self, client_id: str, data: ClientUpdateRequestDTO) -> Client:
        client = await self.get_client(client_id)
        if data.name is not None:
            client.name = data.name
        if data.description is not None:
            client.description = data.description
        if data.scopes is not None:
            client.scopes = data.scopes
        if data.status is not None:
            client.status = data.status.value
        await self._repository.commit()
        await self._repository.refresh(client)
        return client

    async def add_secret(self, client_id: str) -> tuple[ClientSecret, str]:
        client = await self.get_client(client_id)
        active_count = await self._repository.count_active_secrets(client.id)
        if active_count >= _MAX_ACTIVE_SECRETS_PER_CLIENT:
            raise TooManyActiveSecretsError(client_id)

        plaintext_secret = generate_secret()
        secret = ClientSecret(
            client_pk=client.id, secret_hash=hash_secret(plaintext_secret), secret_hint=secret_hint(plaintext_secret)
        )
        self._repository.add_secret(secret)
        await self._repository.commit()
        await self._repository.refresh(secret)
        return secret, plaintext_secret

    async def list_secrets(self, client_id: str) -> list[ClientSecret]:
        client = await self.get_client(client_id)
        return await self._repository.list_secrets(client.id)

    async def revoke_secret(self, client_id: str, secret_id: uuid.UUID) -> None:
        import datetime as _datetime

        client = await self.get_client(client_id)
        secret = await self._repository.get_secret_by_id(secret_id)
        if secret is None or secret.client_pk != client.id:
            raise ClientSecretNotFoundError(secret_id)
        if secret.revoked_at is not None:
            return

        is_this_secret_active = secret.expires_at is None or secret.expires_at > _datetime.datetime.now(
            _datetime.timezone.utc
        )
        if is_this_secret_active and client.status == "active":
            active_count = await self._repository.count_active_secrets(client.id)
            if active_count <= 1:
                raise LastActiveSecretError(client_id)

        secret.revoked_at = _datetime.datetime.now(_datetime.timezone.utc)
        await self._repository.commit()
