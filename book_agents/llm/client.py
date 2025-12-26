import asyncio
from time import perf_counter
from openai import OpenAI
from pydantic import BaseModel
from book_agents.config import settings
from book_agents.db.engine import SessionLocal
from book_agents.db.repo import Repo

class LLMRefusal(RuntimeError):
    pass

class LLMClient:
    def __init__(self):
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY manquant dans .env")
        self.client = OpenAI(api_key=settings.openai_api_key)

    async def structured(
        self,
        output_model: type[BaseModel],
        *,
        instructions: str,
        input_text: str,
        temperature: float = 0.7,
        trace_step_id: str | None = None,
        agent: str | None = None,
    ) -> BaseModel:
        request_payload = {
            "model": settings.openai_model,
            "input": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
            "temperature": temperature,
            "text_format": getattr(output_model, "__name__", "PydanticModel"),
        }

        call_id: str | None = None
        if trace_step_id and agent:
            async with SessionLocal() as s:
                repo = Repo(s)
                call_id = await repo.create_llm_call_started(
                    trace_step_id,
                    agent=agent,
                    model=settings.openai_model,
                    request=request_payload,
                )

        t0 = perf_counter()
        resp = None
        response_payload: dict = {}
        output_text: str | None = None
        parsed = None
        err: str | None = None
        status = "ok"

        def _call():
            return self.client.responses.parse(
                model=settings.openai_model,
                input=request_payload["input"],
                text_format=output_model,
                temperature=temperature,
            )

        try:
            resp = await asyncio.to_thread(_call)
            output_text = getattr(resp, "output_text", None)

            for item in getattr(resp, "output", []) or []:
                if isinstance(item, dict) and item.get("type") == "message":
                    for c in item.get("content", []) or []:
                        if isinstance(c, dict) and c.get("type") == "refusal":
                            raise LLMRefusal(c.get("refusal", "Refusal"))

            parsed = getattr(resp, "output_parsed", None)
            if parsed is None:
                raise RuntimeError("Aucun output_parsed retourné par responses.parse")

            if hasattr(resp, "model_dump"):
                response_payload = resp.model_dump()
            else:
                response_payload = {"repr": repr(resp)}

            return parsed

        except Exception as e:
            status = "error"
            err = str(e)
            if resp is not None:
                if hasattr(resp, "model_dump"):
                    response_payload = resp.model_dump()
                else:
                    response_payload = {"repr": repr(resp)}
            else:
                response_payload = {"error": err}
            raise

        finally:
            latency_ms = int((perf_counter() - t0) * 1000)
            if call_id:
                async with SessionLocal() as s:
                    repo = Repo(s)
                    await repo.finish_llm_call(
                        call_id,
                        status=status,
                        latency_ms=latency_ms,
                        response=response_payload,
                        output_parsed=parsed.model_dump() if hasattr(parsed, "model_dump") else None,
                        output_text=output_text,
                        error=err,
                    )
