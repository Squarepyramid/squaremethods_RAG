"""
SquareMethods - Location Hierarchy Management Service
=======================================================
Place this file at: app/services/manage_locations.py

Handles restructuring an existing location hierarchy -- moving a
location to a new parent, or inserting a new level directly above
one. This is deliberately separate from the equipment import
service: importing equipment only ever creates locations that don't
exist yet (safe, additive). Restructuring an existing hierarchy is a
different kind of operation and needs to be explicit, not inferred
from a path that happens to look different on a later import.

Both operations are cheap and safe for anything nested underneath
the location being moved: every child location and every piece of
equipment anywhere in that subtree references its parent purely by
id, never by path, so moving or re-parenting one node carries its
entire subtree along automatically. Only that one location's
parent_location_id changes.
"""

import uuid
import logging
from typing import Optional

from app.utils.db import get_db_connection

log = logging.getLogger(__name__)


# ── Path lookup (strict -- every level must already exist) ───────────────────

def _find_location_id_by_path(conn, path: list[str], company_id: str) -> str:
    """
    Walks an existing path level by level, matching each level on
    (company_id, name, parent_location_id). Unlike the equipment
    import's resolver, this does NOT create anything -- every level
    must already exist, or this raises, since restructuring should
    never silently create part of a path it can't find.
    """
    parent_id = None
    for i, name in enumerate(path):
        with conn.cursor() as cur:
            if parent_id is None:
                cur.execute("""
                    SELECT id FROM locations
                    WHERE company_id = %s::uuid
                    AND parent_location_id IS NULL
                    AND lower(name) = %s
                """, (company_id, name.lower()))
            else:
                cur.execute("""
                    SELECT id FROM locations
                    WHERE company_id = %s::uuid
                    AND parent_location_id = %s::uuid
                    AND lower(name) = %s
                """, (company_id, parent_id, name.lower()))
            row = cur.fetchone()

        if not row:
            found_so_far = " > ".join(path[:i]) or "(top level)"
            raise ValueError(
                f"Location path not found: '{name}' has no match under "
                f"'{found_so_far}'. Restructuring only works on locations "
                f"that already exist."
            )
        parent_id = str(row["id"])

    return parent_id


def _is_descendant(conn, candidate_id: str, of_location_id: str) -> bool:
    """
    True if candidate_id is of_location_id itself, or anywhere in its
    subtree. Used to block moves that would create a cycle (e.g.
    trying to make a location a child of its own descendant).
    """
    current = candidate_id
    seen = set()
    while current is not None:
        if current == of_location_id:
            return True
        if current in seen:
            return False  # defensive: shouldn't happen, avoids infinite loop
        seen.add(current)
        with conn.cursor() as cur:
            cur.execute("SELECT parent_location_id FROM locations WHERE id = %s::uuid", (current,))
            row = cur.fetchone()
        current = str(row["parent_location_id"]) if row and row["parent_location_id"] else None
    return False


# ── Core operation ────────────────────────────────────────────────────────────

def move_location(
    conn,
    company_id: str,
    location_path: list[str],
    new_parent_path: Optional[list[str]],
) -> dict:
    """
    Moves an existing location (found by its current full path) to a
    new parent (found by its full path, or None to make it top-level).

    Everything nested under the location -- child locations at any
    depth, and all equipment attached anywhere in that subtree --
    moves with it automatically, since none of them are touched;
    only the location's own parent_location_id changes.

    Returns {"location_id": ..., "old_parent_id": ..., "new_parent_id": ...}
    """
    location_id = _find_location_id_by_path(conn, location_path, company_id)

    new_parent_id = None
    if new_parent_path:
        new_parent_id = _find_location_id_by_path(conn, new_parent_path, company_id)

        if new_parent_id == location_id:
            raise ValueError("A location cannot be moved under itself.")
        if _is_descendant(conn, new_parent_id, location_id):
            raise ValueError(
                "Cannot move a location under one of its own descendants -- "
                "this would create a cycle in the hierarchy."
            )

    with conn.cursor() as cur:
        cur.execute("SELECT parent_location_id FROM locations WHERE id = %s::uuid", (location_id,))
        old_parent_row = cur.fetchone()
    old_parent_id = str(old_parent_row["parent_location_id"]) if old_parent_row and old_parent_row["parent_location_id"] else None

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE locations
            SET parent_location_id = %s::uuid, updated_at = NOW()
            WHERE id = %s::uuid AND company_id = %s::uuid
        """, (new_parent_id, location_id, company_id))

    log.info(
        f"Moved location {location_id} (path: {' > '.join(location_path)}) "
        f"from parent {old_parent_id} to {new_parent_id}"
    )

    return {
        "location_id": location_id,
        "old_parent_id": old_parent_id,
        "new_parent_id": new_parent_id,
    }


def insert_location_level(
    conn,
    company_id: str,
    existing_location_path: list[str],
    new_level_name: str,
) -> dict:
    """
    Inserts a new location level directly above an existing location
    (found by its current full path), without disturbing anything
    else. The new level takes over as that location's parent; the
    location's old parent becomes the new level's parent instead.

    Example: "Plant 1 > Packaging Line 1" (2 levels) becomes
    "Plant 1 > Building A > Packaging Line 1" (3 levels) by calling
    insert_location_level(existing_location_path=["Plant 1", "Packaging Line 1"],
                           new_level_name="Building A").
    Packaging Line 1 keeps its own id, so every child location and
    every piece of equipment under it moves along automatically.

    Returns {"new_level_id": ..., "location_id": ...}
    """
    if len(existing_location_path) == 0:
        raise ValueError("existing_location_path cannot be empty.")

    old_parent_path = existing_location_path[:-1]
    location_id = _find_location_id_by_path(conn, existing_location_path, company_id)

    with conn.cursor() as cur:
        cur.execute("SELECT parent_location_id FROM locations WHERE id = %s::uuid", (location_id,))
        old_parent_row = cur.fetchone()
    old_parent_id = str(old_parent_row["parent_location_id"]) if old_parent_row and old_parent_row["parent_location_id"] else None

    # Create (or reuse, if it already exists under the same old parent)
    # the new intermediate level.
    with conn.cursor() as cur:
        if old_parent_id is None:
            cur.execute("""
                SELECT id FROM locations
                WHERE company_id = %s::uuid AND parent_location_id IS NULL AND lower(name) = %s
            """, (company_id, new_level_name.lower()))
        else:
            cur.execute("""
                SELECT id FROM locations
                WHERE company_id = %s::uuid AND parent_location_id = %s::uuid AND lower(name) = %s
            """, (company_id, old_parent_id, new_level_name.lower()))
        existing_new_level = cur.fetchone()

    if existing_new_level:
        new_level_id = str(existing_new_level["id"])
    else:
        new_level_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO locations
                    (id, company_id, name, parent_location_id, created_at, updated_at)
                VALUES
                    (%s::uuid, %s::uuid, %s, %s::uuid, NOW(), NOW())
            """, (new_level_id, company_id, new_level_name, old_parent_id))

    move_location(conn, company_id, existing_location_path, old_parent_path + [new_level_name])

    log.info(
        f"Inserted level '{new_level_name}' above {' > '.join(existing_location_path)} "
        f"for company {company_id}"
    )

    return {"new_level_id": new_level_id, "location_id": location_id}


# ── Entry points called from the FastAPI endpoints ────────────────────────────

def move(company_id: str, location_path: list[str], new_parent_path: Optional[list[str]]) -> dict:
    conn = get_db_connection()
    try:
        result = move_location(conn, company_id, location_path, new_parent_path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result


def insert_level(company_id: str, existing_location_path: list[str], new_level_name: str) -> dict:
    conn = get_db_connection()
    try:
        result = insert_location_level(conn, company_id, existing_location_path, new_level_name)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result