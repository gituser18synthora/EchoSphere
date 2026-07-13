# Context Manager (Step 4)

Per-call state lives in **Redis** (`session:{tenant_id}:{voicebot_id}:{call_id}`).  
Cross-call memory lives in **MongoDB** collection `caller_graphs` (see `config_layer.db.COLLECTION_CALLER_GRAPHS`).

## Environment

- `REDIS_URL` — required for live calls and `mic_test` (e.g. `redis://localhost:6379/0`).
- `MONGO_URI` / `mongo_db_name` — required as before.

## Manual verification (mic test)

1. Start Redis and MongoDB; set `REDIS_URL` in `.env`.
2. Run: `python -m test_runner.mic_test --voicebot-id <id>`
3. During the call, in `redis-cli`:  
   `GET session:{tenant_id}:{voicebot_id}:{call_id}`  
   Expect JSON with `turns`, `turn_count`, `sentiment_trend`, etc.
4. After `end_call`, in MongoDB:  
   `db.caller_graphs.findOne({ caller_phone: "+91..." })`  
   Expect nodes, edges, `call_history`.
5. After call end, same Redis `GET` should return `(nil)` (session deleted).

## Tests

```bash
cd voicebot
pytest tests/context_manager/ -v
```
