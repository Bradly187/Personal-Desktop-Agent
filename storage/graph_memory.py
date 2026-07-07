"""GraphMemory — Lightweight knowledge graph layer using NetworkX.

Provides an explicitly modeled graph of entities and relationships
to complement the semantic vector space.
"""
from __future__ import annotations

import asyncio
import logging
import json
from typing import Optional, TYPE_CHECKING
import networkx as nx

if TYPE_CHECKING:
    from storage.db import AgentDB

log = logging.getLogger(__name__)

class GraphMemory:
    """In-memory NetworkX DiGraph backed by AgentDB for persistence."""

    def __init__(self, agent_db: Optional["AgentDB"] = None) -> None:
        self._agent_db = agent_db
        self.graph = nx.DiGraph()
        self._sync_task: Optional[asyncio.Task] = None

    async def load_from_db(self) -> None:
        """Load the entire graph from SQLite into memory."""
        if not self._agent_db:
            return
        
        nodes, edges = await self._agent_db.graph.get_knowledge_graph()
        
        # Add nodes
        for n in nodes:
            attrs = json.loads(n["attributes"]) if n.get("attributes") else {}
            self.graph.add_node(
                n["id"], 
                name=n["name"], 
                type=n["type"], 
                **attrs
            )
            
        # Add edges
        for e in edges:
            self.graph.add_edge(
                e["source_id"], 
                e["target_id"], 
                relation=e["relation"], 
                weight=e["weight"]
            )
            
        log.info("GraphMemory loaded %d nodes, %d edges", len(nodes), len(edges))

    async def add_fact(self, source_name: str, source_type: str, relation: str, target_name: str, target_type: str, weight: float = 1.0) -> None:
        """Add a factual relationship between two entities."""
        if not self._agent_db:
            return

        # 1. Ensure nodes exist in DB
        source_id = await self._agent_db.graph.insert_knowledge_node(source_name, source_type)
        target_id = await self._agent_db.graph.insert_knowledge_node(target_name, target_type)

        if source_id < 0 or target_id < 0:
            return

        # 2. Add edge to DB
        await self._agent_db.graph.insert_knowledge_edge(source_id, target_id, relation, weight)

        # 3. Update in-memory graph
        self.graph.add_node(source_id, name=source_name, type=source_type)
        self.graph.add_node(target_id, name=target_name, type=target_type)
        self.graph.add_edge(source_id, target_id, relation=relation, weight=weight)
        
    def get_neighbors(self, node_name: str, depth: int = 1) -> list[dict]:
        """Find immediate neighbors of a node by its name."""
        # Find node ID
        start_id = None
        for n_id, data in self.graph.nodes(data=True):
            if data.get("name") == node_name:
                start_id = n_id
                break
                
        if start_id is None:
            return []
            
        # We only support depth=1 for simplicity right now
        neighbors = []
        for successor_id in self.graph.successors(start_id):
            edge_data = self.graph.get_edge_data(start_id, successor_id)
            node_data = self.graph.nodes[successor_id]
            neighbors.append({
                "name": node_data.get("name"),
                "type": node_data.get("type"),
                "relation": edge_data.get("relation"),
                "weight": edge_data.get("weight")
            })
            
        return neighbors
