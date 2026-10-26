"""Verify model access from the managed Hindsight runtime."""

import asyncio
import logging
import math
import os


async def probe():
    from hindsight_api.engine.embeddings import create_embeddings_from_env
    from hindsight_api.engine.cross_encoder import create_cross_encoder_from_env
    from hindsight_api.engine.llm_wrapper import LLMProvider

    embeddings = create_embeddings_from_env()
    await embeddings.initialize()
    vectors = await embeddings.encode(["连接检查"])
    assert len(vectors) == 1
    assert len(vectors[0]) == int(os.environ["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"])
    assert all(math.isfinite(float(value)) for value in vectors[0])
    llm = LLMProvider.from_env()
    try:
        await llm.verify_connection()
    finally:
        await llm.cleanup()
    if os.environ.get("HINDSIGHT_API_RERANKER_PROVIDER") != "rrf":
        reranker = create_cross_encoder_from_env()
        await reranker.initialize()
        scores = await reranker.predict([("连接检查", "记忆服务连接检查")])
        assert len(scores) == 1 and math.isfinite(float(scores[0]))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(probe())
