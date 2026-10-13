"""POST /internal/prewarm: lets the Conversation service resolve an agent's retrieval plan at call start, so the
first retrieve() of the call finds it cached. Same trust model as /internal/retrieve."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import assert_tenant_access, get_current_user

from .. import retrieval_plan
from ..resilience import RetrievalUnavailable
from ..runtime import get_embedding_manager

router = APIRouter(prefix="/internal", tags=["internal"])


class PrewarmRequest(BaseModel):
    tenant_slug: str
    agent_slug: str


@router.post("/prewarm")
async def prewarm(body: PrewarmRequest, current_user: CurrentUser = Depends(get_current_user)):
    # This route has no path parameter, so tests/test_rls_coverage.py's route walk does not see it:
    # test_retrieval_hardening.py::test_prewarm_route_still_authorises_the_tenant is its guard. Keep this first.
    await assert_tenant_access(body.tenant_slug, current_user)
    set_target_tenant(body.tenant_slug)
    try:
        warmed = await retrieval_plan.prewarm(
            body.tenant_slug, body.agent_slug, embedding_manager=get_embedding_manager(),
        )
    except RetrievalUnavailable:
        return {"warmed": False, "reason": "overloaded"}  # best effort: the first retrieve() pays for the plan
    return {"warmed": warmed}
