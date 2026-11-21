"""Verify model access from the managed Hindsight runtime."""

import asyncio
import json
import math
import os


STAGES = (
    "embedding_init", "embedding_encode", "embedding_dim_check", "llm_verify",
    "rerank_init", "rerank_predict", "complete",
)


class ProbeError(ValueError):
    def __init__(self, stage, code):
        self.stage, self.code = stage, code


async def probe():
    stage, last_ok_stage = "embedding_init", None
    try:
        from hindsight_api.engine.embeddings import create_embeddings_from_env

        embeddings = create_embeddings_from_env()
        await embeddings.initialize()

        last_ok_stage, stage = stage, "embedding_encode"
        vectors = await embeddings.encode(["连接检查"])
        if len(vectors) == 0:
            raise ProbeError(stage, "embedding_empty")
        if len(vectors) != 1:
            raise ProbeError(stage, "embedding_invalid_count")
        if len(vectors[0]) == 0:
            raise ProbeError(stage, "embedding_empty")

        last_ok_stage, stage = stage, "embedding_dim_check"
        if len(vectors[0]) != int(os.environ["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"]):
            raise ProbeError(stage, "embedding_dim_mismatch")
        if not all(finite(value) for value in vectors[0]):
            raise ProbeError(stage, "embedding_non_finite")

        last_ok_stage, stage = stage, "llm_verify"
        from hindsight_api.engine.llm_wrapper import LLMProvider

        llm = LLMProvider.from_env()
        try:
            await llm.verify_connection()
        finally:
            await llm.cleanup()

        if os.environ.get("HINDSIGHT_API_RERANKER_PROVIDER") != "rrf":
            last_ok_stage, stage = stage, "rerank_init"
            from hindsight_api.engine.cross_encoder import create_cross_encoder_from_env

            reranker = create_cross_encoder_from_env()
            await reranker.initialize()

            last_ok_stage, stage = stage, "rerank_predict"
            scores = await reranker.predict([("连接检查", "记忆服务连接检查")])
            if len(scores) != 1 or not finite(scores[0]):
                raise ProbeError(stage, "rerank_invalid_score")
    except ProbeError as error:
        return {"ok": False, "stage": error.stage, "last_ok_stage": last_ok_stage, "code": error.code}
    except Exception:
        return {"ok": False, "stage": stage, "last_ok_stage": last_ok_stage, "code": f"{stage}_failed"}
    return {"ok": True, "stage": "complete", "last_ok_stage": stage, "code": "ok"}


def finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def main():
    with os.fdopen(os.dup(1), "w", encoding="utf-8") as output, open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        result = asyncio.run(probe())
        output.write(json.dumps(result) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
