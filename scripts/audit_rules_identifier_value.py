"""Read-only audit to run BEFORE applying migration 0006, which drops
`rules.identifier_value`. Reports every row that still has a non-null value
in that column, so the data loss is visible/auditable rather than silent —
per product decision, this data is intentionally discarded, not migrated
elsewhere.

Usage: ./venv/bin/python scripts/audit_rules_identifier_value.py
"""
import asyncio

from sqlalchemy import text

from core.settings import settings
from sqlalchemy.ext.asyncio import create_async_engine


async def main() -> None:
    engine = create_async_engine(settings.get_database_url())
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id, endpoint, identifier_type, identifier_value "
                "FROM rules WHERE identifier_value IS NOT NULL "
                "ORDER BY endpoint, identifier_type"
            )
        )
        rows = result.fetchall()
    await engine.dispose()

    if not rows:
        print("No rows with a non-null identifier_value. Safe to drop the column.")
        return

    print(f"{len(rows)} row(s) will lose their identifier_value on migration 0006:")
    for row in rows:
        print(f"  id={row.id} endpoint={row.endpoint!r} identifier_type={row.identifier_type!r} "
              f"identifier_value={row.identifier_value!r}")


if __name__ == "__main__":
    asyncio.run(main())
