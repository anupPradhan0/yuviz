"""
Knowledge SDK — the only component allowed to know where retrieved context
comes from (Redis availability flag vs. Knowledge Service's internal REST
API vs. pgvector). Conversation Service, and any future service, should
depend only on IKnowledgeProvider and the models below — never on
services.knowledge directly, never on redis-py/httpx directly.

Typical construction (see services/conversation/__main__.py):

    availability_repo = RedisKnowledgeRepository(os.environ["REDIS_URL"])
    retrieval_repo = HttpKnowledgeRepository(
        base_url=os.environ["KNOWLEDGE_SERVICE_URL"],
        auth_base_url=os.environ["CONFIG_SERVICE_URL"],  # JWTs are minted by Config Service only
        service_email=os.environ["CONFIG_SERVICE_EMAIL"],
        service_password=os.environ["CONFIG_SERVICE_PASSWORD"],
    )
    knowledge: IKnowledgeProvider = CacheAsideKnowledgeProvider(availability_repo, retrieval_repo)

    context = await knowledge.retrieve(tenant_slug, agent_slug, query)
"""

from .interfaces import IKnowledgeAvailabilityRepository, IKnowledgeProvider, IRetrievalRepository
from .models import ChunkSource, RetrievalPolicy, RetrievedChunk, RetrievedContext
from .providers import CacheAsideKnowledgeProvider, MockKnowledgeProvider
from .repositories import HttpKnowledgeRepository, RedisKnowledgeRepository

__all__ = [
    "IKnowledgeProvider",
    "IKnowledgeAvailabilityRepository",
    "IRetrievalRepository",
    "RetrievalPolicy",
    "ChunkSource",
    "RetrievedChunk",
    "RetrievedContext",
    "CacheAsideKnowledgeProvider",
    "MockKnowledgeProvider",
    "RedisKnowledgeRepository",
    "HttpKnowledgeRepository",
]
