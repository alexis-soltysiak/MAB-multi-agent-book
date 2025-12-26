import asyncio
import orjson
from dataclasses import dataclass
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from book_agents.domain.types import Envelope

@dataclass
class StreamMessage:
    redis_id: str
    envelope: Envelope

class RedisStreamsBus:
    def __init__(self, redis: Redis, stream_key: str, dlq_stream_key: str):
        self._redis = redis
        self._stream_key = stream_key
        self._dlq_stream_key = dlq_stream_key

    async def ensure_group(self, group: str) -> None:
        try:
            await self._redis.xgroup_create(name=self._stream_key, groupname=group, id="0-0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                return
            raise

    async def read_forever(self, group: str, consumer: str, block_ms: int = 5000, count: int = 10, min_idle_ms: int = 1000):
        await self.ensure_group(group)

        while True:
            try:
                resp = await self._redis.xautoclaim(
                    name=self._stream_key,
                    groupname=group,
                    consumername=consumer,
                    min_idle_time=min_idle_ms,
                    start_id="0-0",
                    count=count,
                )
                next_start_id, messages, _ = resp[0], resp[1], resp[2] if len(resp) > 2 else (resp[0], resp[1], None)

                if messages:
                    for redis_id, fields in messages:
                        yield StreamMessage(redis_id=str(redis_id), envelope=_to_envelope(fields))
                    continue

                resp2 = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={self._stream_key: ">"},
                    count=count,
                    block=block_ms,
                )
                if not resp2:
                    await asyncio.sleep(0)
                    continue

                for _, messages2 in resp2:
                    for redis_id, fields in messages2:
                        yield StreamMessage(redis_id=str(redis_id), envelope=_to_envelope(fields))

            except Exception:
                await asyncio.sleep(1)

    async def ack(self, group: str, redis_id: str) -> None:
        await self._redis.xack(self._stream_key, group, redis_id)
        

    async def publish(self, env: Envelope) -> None:
        await self._redis.xadd(
            self._stream_key,
            {
                "event_id": env.event_id,
                "event_type": env.event_type,
                "book_id": env.book_id,
                "created_at": env.created_at.isoformat(),
                "payload": orjson.dumps(env.payload).decode("utf-8"),
                "meta": orjson.dumps(env.meta).decode("utf-8"),
            },
        )

    async def publish_dlq(self, env: Envelope, error: dict) -> None:
        await self._redis.xadd(
            self._dlq_stream_key,
            {
                "event_id": env.event_id,
                "event_type": env.event_type,
                "book_id": env.book_id,
                "created_at": env.created_at.isoformat(),
                "payload": orjson.dumps(env.payload).decode("utf-8"),
                "meta": orjson.dumps(env.meta).decode("utf-8"),
                "error": orjson.dumps(error).decode("utf-8"),
            },
        )


def _to_envelope(fields: dict) -> Envelope:
    created_at = str(fields.get("created_at", ""))
    return Envelope(
        event_id=str(fields.get("event_id", "")),
        event_type=str(fields.get("event_type", "")),
        book_id=str(fields.get("book_id", "")),
        created_at=_parse_dt(created_at),
        payload=_safe_json(fields.get("payload", "{}")),
        meta=_safe_json(fields.get("meta", "{}")),
    )

def _safe_json(v) -> dict:
    try:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", errors="replace")
        return orjson.loads(str(v).encode("utf-8"))
    except Exception:
        return {}

def _parse_dt(s: str):
    from datetime import datetime
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.utcnow()


