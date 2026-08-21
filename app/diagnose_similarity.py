"""
One-off diagnostic, not part of the app.

Queries the real knowledge_embeddings.equipment_id column (see
migration_002_knowledge_embeddings_equipment_id.sql), matching exactly what
retrieval.py's semantic_search() filters on now -- NOT the old
`content LIKE '%equipment_id:{id}%' AND source_type='manual'` text-prefix
hack this script used before that migration landed. Keeping this on the old
filter would silently diagnose a different (and now stale) row population
than what the live query actually searches, so it's been updated to match:
no source_type restriction either, since equipment_id can in principle be
populated on any source_type now, not just 'manual'.

19 manual chunks are confirmed to exist in knowledge_embeddings for this
equipment (equipment_id 25070e8c-4118-4f04-9b61-c6a9e6a869b9 -- see the
psql check that returned count=19), so the earlier "ingestion produced
nothing" hypothesis is ruled out. The remaining question is why
retrieval.py's semantic_search() isn't surfacing any of them for queries
like "how do i trouble flight bar not moving" -- either:

  (a) SEMANTIC_SIMILARITY_THRESHOLD = 0.7 in retrieval.py is too strict
      for this embedding model/these queries and is throwing away real
      matches, or
  (b) the embeddings aren't comparable at all -- e.g. ingestion and query
      time used different embedding models or dimensions, which tends to
      produce uniformly low (or nonsensically uniform) cosine similarity
      across every row regardless of actual topical relevance.

This script bypasses both semantic_search()'s LIMIT 5 and its 0.7 cutoff
and prints the real similarity score against EVERY manual chunk stored
for the given equipment, ranked. That distribution answers which of (a)
or (b) it is:
  - scores spread out with the flight-bar/main-drive chunk clearly on
    top but under 0.7            -> threshold is set too high, lower it
  - every score is roughly the same, clustered near 0 (or near 1)
    regardless of content        -> embeddings aren't comparable, check
                                     which model get_embedding() calls
                                     vs. what ingestion used
  - the relevant chunk isn't in the list at all despite scoring OK      -> LIKE filter/equipment_id
                                     tagging bug, not a similarity issue

Usage (run in the real app environment, with DB + embedding access):
    python3 diagnose_similarity.py 25070e8c-4118-4f04-9b61-c6a9e6a869b9 "how do i trouble flight bar not moving"
    python3 diagnose_similarity.py 25070e8c-4118-4f04-9b61-c6a9e6a869b9 "what is the working principle of this machine"
"""
import sys

from app.services.embeddings import get_embedding
from app.utils.db import get_db_connection


def main(equipment_id: str, query: str):
    embedding = get_embedding(query)
    embedding_str = "[" + ",".join(map(str, embedding)) + "]"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT left(content, 140) AS preview,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM knowledge_embeddings
                WHERE equipment_id = %s::uuid
                ORDER BY embedding <=> %s::vector
            """, (embedding_str, equipment_id, embedding_str))
            rows = cur.fetchall()
    finally:
        conn.close()

    print(f"query: {query!r}")
    print(f"{len(rows)} chunks found for equipment_id={equipment_id} (any source_type)\n")
    for row in rows:
        flag = "  <-- would pass 0.7 threshold" if row["similarity"] > 0.7 else ""
        print(f"{row['similarity']:.4f}  {row['preview']!r}{flag}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python3 {sys.argv[0]} <equipment_id> \"<query text>\"")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])