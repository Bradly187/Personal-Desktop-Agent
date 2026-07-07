from __future__ import annotations
import json
import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

class GraphRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_knowledge_node(self, name: str, node_type: str, attributes: dict = None) -> int:  # type: ignore[assignment]
        if not self._conn:
            return -1
        try:
            attrs_json = json.dumps(attributes) if attributes else None
            async with self._conn.execute(
                """INSERT INTO knowledge_nodes (name, type, attributes)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET type=excluded.type, attributes=excluded.attributes
                   RETURNING id""",
                (name, node_type, attrs_json)
            ) as cur:
                row = await cur.fetchone()
                await self._conn.commit()
                return row["id"] if row else -1
        except Exception as exc:
            log.warning("AgentDB.insert_knowledge_node failed: %s", exc)
            return -1

    async def insert_knowledge_edge(self, source_id: int, target_id: int, relation: str, weight: float = 1.0) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT OR REPLACE INTO knowledge_edges (source_id, target_id, relation, weight)
                   VALUES (?, ?, ?, ?)""",
                (source_id, target_id, relation, weight)
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_knowledge_edge failed: %s", exc)

    async def get_knowledge_graph(self) -> tuple[list[dict], list[dict]]:
        if not self._conn:
            return [], []
        try:
            async with self._conn.execute("SELECT id, name, type, attributes FROM knowledge_nodes") as cur:
                nodes = [dict(r) for r in await cur.fetchall()]
            async with self._conn.execute("SELECT source_id, target_id, relation, weight FROM knowledge_edges") as cur:
                edges = [dict(r) for r in await cur.fetchall()]
            return nodes, edges
        except Exception as exc:
            log.warning("AgentDB.get_knowledge_graph failed: %s", exc)
            return [], []

