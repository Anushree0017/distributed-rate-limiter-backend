"""Bootstrap CLI: creates the first admin client directly against Postgres,
bypassing the API entirely (the clients admin API itself requires an admin
token — something must exist before it can create anything). Prints the
plaintext secret exactly once; it's never stored or retrievable again.

Usage:
    ./venv/bin/python scripts/create_client.py <client_id> <name> --scopes check admin
"""
import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.security.secrets import generate_secret, hash_secret, secret_hint
from core.settings import settings
from model.client import Client
from model.client_secret import ClientSecret
from repositories.client_repository import ClientRepository


async def main(client_id: str, name: str, description: str | None, scopes: list[str]) -> None:
    engine = create_async_engine(settings.get_database_url())
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        repo = ClientRepository(session)
        existing = await repo.get_by_client_id(client_id)
        if existing is not None:
            print(f"A client with client_id={client_id!r} already exists (id={existing.id}). Not creating another.")
            await engine.dispose()
            return

        client = Client(client_id=client_id, name=name, description=description, scopes=scopes)
        repo.add(client)
        await repo.commit()
        await repo.refresh(client)

        plaintext_secret = generate_secret()
        repo.add_secret(
            ClientSecret(
                client_pk=client.id,
                secret_hash=hash_secret(plaintext_secret),
                secret_hint=secret_hint(plaintext_secret),
            )
        )
        await repo.commit()

    await engine.dispose()

    print(f"Created client client_id={client_id!r} (id={client.id}) with scopes={scopes}.")
    print("Client secret (shown once, not recoverable afterward):")
    print(f"  {plaintext_secret}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_id", help="Public slug, e.g. 'gateway' or 'admin-cli' (^[a-z0-9][a-z0-9-]{2,62}$)")
    parser.add_argument("name", help="Human-readable name")
    parser.add_argument("--description", default=None)
    parser.add_argument(
        "--scopes", nargs="+", choices=["check", "admin"], default=["admin"], help="Default: admin"
    )
    args = parser.parse_args()
    asyncio.run(main(args.client_id, args.name, args.description, args.scopes))
