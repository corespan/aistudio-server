"""
Seed workload_types from catalog.json on first boot.

Usage:
    # As a module (from Makefile / docker compose exec):
    python -m app.services.catalog_seeder

    # Programmatically (from app lifespan or test fixtures):
    from app.services.catalog_seeder import seed_catalog
    seed_catalog()

Idempotent: safe to run multiple times.
  - New workload types are inserted.
  - Existing ones have display_name, description, and image_tag updated if changed.
"""

import json
import logging
import os

from app.database import SyncSessionLocal
from app.models.workload_type import WorkloadType

logger = logging.getLogger(__name__)

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "catalog.json")


def seed_catalog(catalog_path: str = CATALOG_PATH) -> int:
    """
    Read catalog.json and upsert workload_types into PostgreSQL.
    Returns the number of new types inserted.
    """
    catalog_path = os.path.abspath(catalog_path)

    if not os.path.exists(catalog_path):
        logger.warning("catalog.json not found at %s — skipping seed", catalog_path)
        return 0

    with open(catalog_path) as f:
        catalog = json.load(f)

    workload_types = catalog.get("workload_types", [])
    inserted = 0

    with SyncSessionLocal() as db:
        for wt in workload_types:
            existing = (
                db.query(WorkloadType)
                .filter(WorkloadType.name == wt["name"])
                .first()
            )
            if existing:
                # Update mutable fields — image_tag can change between releases
                existing.display_name = wt["display_name"]
                existing.description = wt.get("description", "")
                existing.image_tag = wt.get("image_tag")
                logger.info(
                    "Updated workload type: %s  image_tag=%s",
                    wt["name"], wt.get("image_tag"),
                )
            else:
                db.add(WorkloadType(
                    name=wt["name"],
                    display_name=wt["display_name"],
                    description=wt.get("description", ""),
                    image_tag=wt.get("image_tag"),
                ))
                logger.info(
                    "Seeded workload type: %s  image_tag=%s",
                    wt["name"], wt.get("image_tag"),
                )
                inserted += 1
        db.commit()

    logger.info("Catalog seed complete. %d new type(s) inserted.", inserted)
    return inserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_catalog()
