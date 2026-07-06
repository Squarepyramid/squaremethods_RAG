"""
SquareMethods - Equipment Master Data Import Service
=====================================================
Place this file at: app/services/import_equipment_master_data.py

Parses the Equipment Master Data Excel template and creates
locations, equipment_types, and equipment rows directly in the
database for a given company. No async ingestion queue -- this
runs synchronously end to end, called from a FastAPI endpoint.

The template has three tabs:
  "Locations"       -> Location Name *, Parent Location Name
  "Equipment Types"  -> Equipment Type Name *
  "Equipment"        -> Equipment Name *, Location Name,
                         Equipment Type Name *, Reference Code *, Notes

Locations are resolved parent-first so nested hierarchies of any
depth load correctly. Equipment Types are resolved against the
company's existing types, then against equipment_type_defaults
(to link source_default_id), and created fresh only if no match
is found anywhere. Equipment reference_code is enforced unique per
company before insert.
"""

import io
import uuid
import logging

from openpyxl import load_workbook

from app.utils.db import get_db_connection

log = logging.getLogger(__name__)

REQUIRED_SHEETS = ["Locations", "Equipment Types", "Equipment"]


# ── Excel parser ──────────────────────────────────────────────────────────────

def parse_excel(file_bytes: bytes) -> dict:
    """
    Parse the Equipment Master Data Excel into three lists of dicts:
    {
        "locations": [ {"name": str, "parent_name": str|None}, ... ],
        "equipment_types": [ {"name": str}, ... ],
        "equipment": [ {"name": str, "location_name": str|None,
                         "type_name": str, "reference_code": str,
                         "notes": str|None}, ... ],
    }

    Skips completely empty rows. Assumes row 1 of each tab is the
    header row (matching the template's column order).
    """
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)

    for sheet in REQUIRED_SHEETS:
        if sheet not in wb.sheetnames:
            raise ValueError(f"Missing required tab '{sheet}' in uploaded file.")

    locations = []
    for row in wb["Locations"].iter_rows(min_row=2, values_only=True):
        if not any(cell for cell in row if cell is not None):
            continue
        name = str(row[0]).strip() if row[0] is not None else ""
        if not name:
            continue
        parent_raw = row[1] if len(row) > 1 else None
        parent_name = str(parent_raw).strip() if parent_raw is not None else None
        locations.append({"name": name, "parent_name": parent_name or None})

    equipment_types = []
    for row in wb["Equipment Types"].iter_rows(min_row=2, values_only=True):
        if not any(cell for cell in row if cell is not None):
            continue
        name = str(row[0]).strip() if row[0] is not None else ""
        if not name:
            continue
        equipment_types.append({"name": name})

    equipment = []
    for row in wb["Equipment"].iter_rows(min_row=2, values_only=True):
        if not any(cell for cell in row if cell is not None):
            continue
        name = str(row[0]).strip() if row[0] is not None else ""
        if not name:
            continue
        location_raw = row[1] if len(row) > 1 else None
        location_name = str(location_raw).strip() if location_raw is not None else None
        type_name = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
        reference_code = str(row[3]).strip() if len(row) > 3 and row[3] is not None else ""
        notes_raw = row[4] if len(row) > 4 else None
        notes = str(notes_raw).strip() if notes_raw is not None else None

        equipment.append({
            "name": name,
            "location_name": location_name or None,
            "type_name": type_name,
            "reference_code": reference_code,
            "notes": notes,
        })

    log.info(
        f"Parsed {len(locations)} locations, {len(equipment_types)} equipment types, "
        f"{len(equipment)} equipment rows from Excel"
    )
    return {
        "locations": locations,
        "equipment_types": equipment_types,
        "equipment": equipment,
    }


# ── Validation (within-file checks only; DB checks happen during save) ───────

def validate(parsed: dict) -> list[str]:
    errors = []

    loc_names = {loc["name"].lower() for loc in parsed["locations"]}
    for loc in parsed["locations"]:
        if loc["parent_name"] and loc["parent_name"].lower() not in loc_names:
            errors.append(
                f"Location '{loc['name']}': parent '{loc['parent_name']}' not found in Locations tab."
            )

    ref_codes = [e["reference_code"] for e in parsed["equipment"] if e["reference_code"]]
    seen = set()
    for code in ref_codes:
        if code in seen:
            errors.append(f"Duplicate Reference Code within file: '{code}'")
        seen.add(code)

    for e in parsed["equipment"]:
        if not e["reference_code"]:
            errors.append(f"Equipment '{e['name']}': Reference Code is required.")
        if not e["type_name"]:
            errors.append(f"Equipment '{e['name']}': Equipment Type Name is required.")

    return errors


# ── DB helpers ────────────────────────────────────────────────────────────────

def resolve_or_create_locations(conn, locations: list[dict], company_id: str) -> dict:
    """
    Inserts locations parent-first so any depth of nesting resolves
    correctly. Returns a map of lower(name) -> location id (str) for
    every location, existing or newly created, for this company.
    """
    id_map = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM locations WHERE company_id = %s::uuid",
            (company_id,),
        )
        for row in cur.fetchall():
            id_map[row["name"].lower()] = str(row["id"])

    remaining = list(locations)
    progress = True
    while remaining and progress:
        progress = False
        still_pending = []
        for loc in remaining:
            key = loc["name"].lower()
            if key in id_map:
                continue
            parent_key = loc["parent_name"].lower() if loc["parent_name"] else None
            if parent_key and parent_key not in id_map:
                still_pending.append(loc)
                continue

            new_id = str(uuid.uuid4())
            parent_id = id_map.get(parent_key) if parent_key else None
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO locations
                        (id, company_id, name, parent_location_id, created_at, updated_at)
                    VALUES
                        (%s::uuid, %s::uuid, %s, %s::uuid, NOW(), NOW())
                """, (new_id, company_id, loc["name"], parent_id))
            id_map[key] = new_id
            progress = True
        remaining = still_pending

    if remaining:
        unresolved = [loc["name"] for loc in remaining]
        raise ValueError(f"Could not resolve parent chain for locations: {unresolved}")

    log.info(f"Resolved {len(id_map)} locations for company {company_id}")
    return id_map


def resolve_or_create_equipment_type(conn, type_name: str, company_id: str, type_id_map: dict) -> str:
    """
    Resolves an equipment type name to an id, creating it if needed.
    Checks the company's existing equipment_types first, then
    equipment_type_defaults (to link source_default_id), then
    creates a fresh company-scoped type as a last resort.
    Mutates and reuses type_id_map as a cache across calls.
    """
    key = type_name.lower()
    if key in type_id_map:
        return type_id_map[key]

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM equipment_types
            WHERE company_id = %s::uuid AND lower(name) = %s
        """, (company_id, key))
        row = cur.fetchone()

    if row:
        type_id_map[key] = str(row["id"])
        return type_id_map[key]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM equipment_type_defaults WHERE lower(name) = %s",
            (key,),
        )
        default_row = cur.fetchone()

    source_default_id = str(default_row["id"]) if default_row else None
    new_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO equipment_types
                (id, company_id, name, source_default_id, created_at, updated_at)
            VALUES
                (%s::uuid, %s::uuid, %s, %s, NOW(), NOW())
        """, (new_id, company_id, type_name, source_default_id))

    type_id_map[key] = new_id
    return new_id


def save_equipment(
    conn,
    equipment: list[dict],
    location_id_map: dict,
    company_id: str,
) -> list[dict]:
    """
    Inserts equipment rows, resolving location and equipment type
    references and enforcing reference_code uniqueness per company.
    Returns a summary list of created equipment.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT reference_code FROM equipment WHERE company_id = %s::uuid",
            (company_id,),
        )
        existing_ref_codes = {row["reference_code"] for row in cur.fetchall()}

    type_id_map = {}
    created = []

    for e in equipment:
        if e["reference_code"] in existing_ref_codes:
            raise ValueError(
                f"Equipment '{e['name']}': reference_code '{e['reference_code']}' "
                f"already exists for this company."
            )

        location_id = None
        if e["location_name"]:
            location_id = location_id_map.get(e["location_name"].lower())
            if location_id is None:
                raise ValueError(
                    f"Equipment '{e['name']}': location '{e['location_name']}' could not be resolved."
                )

        equipment_type_id = resolve_or_create_equipment_type(
            conn, e["type_name"], company_id, type_id_map
        )

        new_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO equipment
                    (id, company_id, equipment_type_id, location_id, name,
                     reference_code, notes, status, created_at, updated_at)
                VALUES
                    (%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s,
                     %s, %s, 'draft', NOW(), NOW())
            """, (
                new_id, company_id, equipment_type_id, location_id,
                e["name"], e["reference_code"], e["notes"],
            ))

        existing_ref_codes.add(e["reference_code"])
        created.append({
            "equipment_id": new_id,
            "name": e["name"],
            "reference_code": e["reference_code"],
        })

    log.info(f"Created {len(created)} equipment rows for company {company_id}")
    return created


# ── Main entry point ──────────────────────────────────────────────────────────

def ingest(file_bytes: bytes, company_id: str, created_by: str) -> dict:
    """
    Full import pipeline called from the FastAPI endpoint.
    1. Parse Excel into locations, equipment_types, equipment
    2. Validate references within the file
    3. Resolve/create locations (parent-first)
    4. Resolve/create equipment (types resolved per-row) and insert
    5. Commit as a single transaction; roll back entirely on any error
    """
    parsed = parse_excel(file_bytes)

    errors = validate(parsed)
    if errors:
        raise ValueError("Validation failed:\n" + "\n".join(f"- {e}" for e in errors))

    conn = get_db_connection()

    try:
        location_id_map = resolve_or_create_locations(conn, parsed["locations"], company_id)
        created_equipment = save_equipment(
            conn, parsed["equipment"], location_id_map, company_id
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log.info(
        f"Import complete. {len(created_equipment)} equipment created for company "
        f"{company_id} (imported by {created_by})"
    )

    return {
        "company_id": company_id,
        "locations_resolved": len(location_id_map),
        "equipment_created": len(created_equipment),
        "equipment": created_equipment,
    }