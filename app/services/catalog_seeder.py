"""
Seed workload_types from catalog.json on first boot.

Usage:
    # As a module (from Makefile / docker compose exec):
    python -m app.services.catalog_seeder

    # Programmatically (from app lifespan or test fixtures):
    from app.services.catalog_seeder import seed_catalog
    seed_catalog()

Idempotent: safe to run multiple times. Uses INSERT ... ON CONFLICT DO NOTHING.
"""

import json
import logging
import os

from sqlalchemy import text
from app.database import SyncSessionLocal
from app.models.workload_type import WorkloadType

logger = logging.getLogger(__name__)

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "catalog.json")


def seed_catalog(catalog_path: str = CATALOG_PATH) -> int:
    """
    Read catalog.json and upsert workload_types into PostgreSQL.
    Returns the number of types seeded.
    """
    catalog_path = os.path.abspath(catalog_path)

    if not os.path.exists(catalog_path):
        logger.warning("catalog.json not found at %s — skipping seed", catalog_path)
        return 0

    with open(catalog_path) as f:
        catalog = json.load(f)

    workload_types = catalog.get("workload_types", [])
    seeded = 0

    with SyncSessionLocal() as db:
        for wt in workload_types:
            existing = (
                db.query(WorkloadType)
                .filter(WorkloadType.name == wt["name"])
                .first()
            )
            if existing:
                # Update display_name and description if changed
                existing.display_name = wt["display_name"]
                existing.description = wt.get("description", "")
                logger.info("Updated workload type: %s", wt["name"])
            else:
                db.add(WorkloadType(
                    name=wt["name"],
                    display_name=wt["display_name"],
                    description=wt.get("description", ""),
                ))
                logger.info("Seeded workload type: %s", wt["name"])
                seeded += 1
        db.commit()

    logger.info("Catalog seed complete. %d new type(s) added.", seeded)
    return seeded


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_catalog()
