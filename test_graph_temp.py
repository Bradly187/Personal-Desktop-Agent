import asyncio
import os
from storage.db import AgentDB
from storage.graph_memory import GraphMemory

async def test_graph():
    # Use a test DB
    db_path = "test_graph.db"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    db = AgentDB()
    await db.open(db_path)
    
    graph = GraphMemory(db)
    
    # Add facts
    await graph.add_fact("User", "person", "likes", "Python", "language", 1.0)
    await graph.add_fact("Python", "language", "is_used_for", "AI", "field", 1.0)
    
    # Check neighbors
    neighbors = graph.get_neighbors("User")
    print("User neighbors:", neighbors)
    assert len(neighbors) == 1
    assert neighbors[0]["name"] == "Python"
    
    # Close and reload to test DB persistence
    db.close()
    
    db2 = AgentDB()
    await db2.open(db_path)
    graph2 = GraphMemory(db2)
    await graph2.load_from_db()
    
    neighbors2 = graph2.get_neighbors("User")
    print("User neighbors after reload:", neighbors2)
    assert len(neighbors2) == 1
    
    db2.close()
    os.remove(db_path)
    print("All tests passed!")

if __name__ == "__main__":
    asyncio.run(test_graph())
