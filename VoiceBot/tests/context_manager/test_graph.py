"""CallerGraph tests with mongomock behind async wrappers."""

from datetime import datetime, timedelta
import mongomock
import pytest

from config_layer.db import COLLECTION_CALLER_GRAPHS
from context_manager.graph import CallerGraph
from orchestrator.call_state import CallState


class _AsyncCollection:
    """Async façade over mongomock collection (no real I/O)."""

    def __init__(self, coll):
        self._c = coll

    async def find_one(self, filter_doc):
        return self._c.find_one(filter_doc)

    async def update_one(self, filter_doc, update, upsert=False):
        return self._c.update_one(filter_doc, update, upsert=upsert)

    async def delete_one(self, filter_doc):
        return self._c.delete_one(filter_doc)

    async def delete_many(self, filter_doc):
        return self._c.delete_many(filter_doc)


class _FakeDB:
    def __init__(self):
        client = mongomock.MongoClient()
        self._db = client["test_voicebot"]
        self._col = self._db[COLLECTION_CALLER_GRAPHS]

    def __getitem__(self, name: str):
        if name != COLLECTION_CALLER_GRAPHS:
            return _AsyncCollection(self._db[name])
        return _AsyncCollection(self._col)


@pytest.fixture
def mock_mongo_graph(monkeypatch):
    fake = _FakeDB()

    class MongoStub:
        @staticmethod
        def db():
            return fake

    monkeypatch.setattr("context_manager.graph.MongoDB", MongoStub)
    return fake._col


def _state(**kw):
    d = dict(
        call_id="call-1",
        voicebot_id="vb1",
        caller_phone="+91999",
        tenant_id="t1",
    )
    d.update(kw)
    return CallState(**d)


@pytest.mark.asyncio
async def test_load_returns_none_first_time(mock_mongo_graph):
    g = CallerGraph()
    assert await g.load("vb1", "+91999", 30) is None


@pytest.mark.asyncio
async def test_load_returns_graph_existing(mock_mongo_graph):
    now = datetime.utcnow()
    mock_mongo_graph.insert_one({
        "voicebot_id": "vb1",
        "caller_phone": "+91999",
        "tenant_id": "t1",
        "caller_name": "Bob",
        "nodes": [
            {
                "node_id": "f_x",
                "key": "k",
                "value": "v",
                "last_seen": now,
            }
        ],
        "edges": [],
        "call_history": [],
    })
    g = CallerGraph()
    out = await g.load("vb1", "+91999", 30)
    assert out is not None
    assert out["caller_name"] == "Bob"
    assert len(out["nodes"]) == 1


@pytest.mark.asyncio
async def test_load_filters_stale_nodes(mock_mongo_graph):
    old = datetime.utcnow() - timedelta(days=100)
    fresh = datetime.utcnow()
    mock_mongo_graph.insert_one({
        "voicebot_id": "vb1",
        "caller_phone": "+91999",
        "tenant_id": "t1",
        "nodes": [
            {"node_id": "old", "last_seen": old, "key": "a"},
            {"node_id": "new", "last_seen": fresh, "key": "b"},
        ],
        "edges": [
            {
                "from_node": "old",
                "to_node": "old",
                "relation": "discussed",
            },
            {
                "from_node": "new",
                "to_node": "new",
                "relation": "discussed",
            },
        ],
        "call_history": [],
    })
    g = CallerGraph()
    out = await g.load("vb1", "+91999", memory_expiry_days=30)
    ids = {n["node_id"] for n in out["nodes"]}
    assert "new" in ids
    assert "old" not in ids


@pytest.mark.asyncio
async def test_load_max_20_nodes(mock_mongo_graph):
    now = datetime.utcnow()
    nodes = [
        {
            "node_id": f"n{i}",
            "last_seen": now - timedelta(seconds=i),
            "key": str(i),
        }
        for i in range(25)
    ]
    mock_mongo_graph.insert_one({
        "voicebot_id": "vb1",
        "caller_phone": "+91999",
        "tenant_id": "t1",
        "nodes": nodes,
        "edges": [],
        "call_history": [],
    })
    g = CallerGraph()
    out = await g.load("vb1", "+91999", 30)
    assert len(out["nodes"]) == 20


@pytest.mark.asyncio
async def test_load_last_3_call_history(mock_mongo_graph):
    mock_mongo_graph.insert_one({
        "voicebot_id": "vb1",
        "caller_phone": "+91999",
        "tenant_id": "t1",
        "nodes": [],
        "edges": [],
        "call_history": [{"call_id": str(i)} for i in range(5)],
    })
    g = CallerGraph()
    out = await g.load("vb1", "+91999", 30)
    assert len(out["recent_calls"]) == 3
    assert out["recent_calls"][0]["call_id"] == "2"


@pytest.mark.asyncio
async def test_write_creates_doc(mock_mongo_graph):
    g = CallerGraph()
    cs = _state()
    ext = {
        "caller_name": "A",
        "caller_email": "a@b.co",
        "summary": "ok",
        "nodes": [{"node_id": "t_1", "type": "topic", "key": "k", "value": "v"}],
        "edges": [],
    }
    await g.write(cs, ext, memory_expiry_days=30)
    doc = mock_mongo_graph.find_one({"voicebot_id": "vb1", "caller_phone": "+91999"})
    assert doc is not None
    assert doc["caller_name"] == "A"
    assert len(doc["nodes"]) == 1


@pytest.mark.asyncio
async def test_write_upserts_node_value_preserves_list(mock_mongo_graph):
    g = CallerGraph()
    cs = _state()
    mock_mongo_graph.insert_one({
        "voicebot_id": "vb1",
        "caller_phone": "+91999",
        "tenant_id": "t1",
        "nodes": [
            {
                "node_id": "t_1",
                "type": "topic",
                "key": "k",
                "value": "old",
                "first_seen": datetime(2024, 1, 1),
                "last_seen": datetime(2024, 1, 2),
                "confidence": 0.5,
            }
        ],
        "edges": [],
        "call_history": [],
    })
    await g.write(
        cs,
        {
            "summary": "s",
            "nodes": [
                {"node_id": "t_1", "type": "topic", "key": "k", "value": "new"}
            ],
            "edges": [],
        },
        30,
    )
    doc = mock_mongo_graph.find_one({"voicebot_id": "vb1"})
    n = doc["nodes"][0]
    assert n["value"] == "new"
    assert n["first_seen"] == datetime(2024, 1, 1)


@pytest.mark.asyncio
async def test_write_pushes_new_edge_and_updates_relation(mock_mongo_graph):
    g = CallerGraph()
    cs = _state()
    ext1 = {
        "summary": "s",
        "nodes": [{"node_id": "a", "key": "x"}],
        "edges": [{"from_node": "a", "to_node": "a", "relation": "unresolved"}],
    }
    await g.write(cs, ext1, 30)
    ext2 = {
        "summary": "s2",
        "nodes": [{"node_id": "a", "key": "x"}],
        "edges": [{"from_node": "a", "to_node": "a", "relation": "resolved"}],
    }
    await g.write(cs, ext2, 30)
    doc = mock_mongo_graph.find_one({"voicebot_id": "vb1"})
    rels = [e["relation"] for e in doc["edges"]]
    assert "resolved" in rels
    assert sum(1 for r in rels if r == "resolved") >= 1


@pytest.mark.asyncio
async def test_write_caps_call_history(mock_mongo_graph):
    g = CallerGraph()
    cs = _state()
    for i in range(12):
        await g.write(
            cs,
            {"summary": str(i), "nodes": [], "edges": []},
            30,
        )
    doc = mock_mongo_graph.find_one({"voicebot_id": "vb1"})
    assert len(doc["call_history"]) <= 10


@pytest.mark.asyncio
async def test_delete_caller(mock_mongo_graph):
    mock_mongo_graph.insert_one({
        "voicebot_id": "vb1",
        "caller_phone": "+91999",
        "nodes": [],
        "edges": [],
    })
    g = CallerGraph()
    assert await g.delete_caller("vb1", "+91999") is True
    assert mock_mongo_graph.find_one({}) is None


@pytest.mark.asyncio
async def test_delete_caller_unknown_returns_false(mock_mongo_graph):
    g = CallerGraph()
    assert await g.delete_caller("vb1", "+91000") is False


@pytest.mark.asyncio
async def test_delete_node(mock_mongo_graph):
    mock_mongo_graph.insert_one({
        "voicebot_id": "vb1",
        "caller_phone": "+91999",
        "nodes": [
            {"node_id": "n1", "key": "a"},
            {"node_id": "n2", "key": "b"},
        ],
        "edges": [
            {"from_node": "n1", "to_node": "n2", "relation": "x"},
            {"from_node": "n2", "to_node": "n1", "relation": "y"},
        ],
    })
    g = CallerGraph()
    await g.delete_node("vb1", "+91999", "n1")
    doc = mock_mongo_graph.find_one({})
    assert all(n["node_id"] != "n1" for n in doc["nodes"])
    for e in doc["edges"]:
        assert e["from_node"] != "n1" and e["to_node"] != "n1"


@pytest.mark.asyncio
async def test_delete_all_for_voicebot(mock_mongo_graph):
    mock_mongo_graph.insert_many([
        {"voicebot_id": "vb1", "caller_phone": "a"},
        {"voicebot_id": "vb1", "caller_phone": "b"},
        {"voicebot_id": "vb2", "caller_phone": "c"},
    ])
    g = CallerGraph()
    n = await g.delete_all_for_voicebot("vb1")
    assert n == 2
    assert mock_mongo_graph.count_documents({}) == 1


@pytest.mark.asyncio
async def test_write_skips_none_or_empty(mock_mongo_graph):
    g = CallerGraph()
    cs = _state()
    await g.write(cs, None, 30)
    assert mock_mongo_graph.count_documents({}) == 0
    await g.write(cs, {}, 30)
    assert mock_mongo_graph.count_documents({}) == 0
