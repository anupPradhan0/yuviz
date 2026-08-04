from __future__ import annotations

from libs.knowledge_sdk.models import RetrievalPolicy
from libs.knowledge_sdk.providers.mock_provider import MockKnowledgeProvider


async def test_no_chunks_returns_none():
    mock = MockKnowledgeProvider()
    assert await mock.retrieve("acme", "sup", "anything") is None


async def test_add_chunk_and_retrieve_round_trips():
    mock = MockKnowledgeProvider()
    mock.add_chunk("acme", "sup", "Refunds take 30 days.", score=0.9, document_title="Refund Policy")

    result = await mock.retrieve("acme", "sup", "refund policy?")

    assert result is not None
    assert result.chunks[0].content == "Refunds take 30 days."
    assert result.sources == ["Refund Policy"]
    assert result.confidence == 0.9


async def test_top_k_limits_returned_chunks():
    mock = MockKnowledgeProvider()
    for i in range(10):
        mock.add_chunk("acme", "sup", f"chunk {i}", score=i / 10)

    result = await mock.retrieve("acme", "sup", "q", policy=RetrievalPolicy(top_k=3))

    assert result is not None
    assert len(result.chunks) == 3
    assert result.chunks[0].score > result.chunks[1].score > result.chunks[2].score


async def test_minimum_score_filters_low_scoring_chunks():
    mock = MockKnowledgeProvider()
    mock.add_chunk("acme", "sup", "low relevance", score=0.1)

    result = await mock.retrieve("acme", "sup", "q", policy=RetrievalPolicy(minimum_score=0.5))

    assert result is None


async def test_different_agent_has_no_chunks():
    mock = MockKnowledgeProvider()
    mock.add_chunk("acme", "sup", "content")

    assert await mock.retrieve("acme", "other-agent", "q") is None
