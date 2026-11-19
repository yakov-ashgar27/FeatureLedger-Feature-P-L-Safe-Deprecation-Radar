"""When customer text may leave the deployment.

Tier 3 classification embeds support tickets, PR titles and Slack-style text. With an external
OpenAI-compatible endpoint that text is sent to a third party, so a key alone is not enough: the
operator must also set LLM_ALLOW_EXTERNAL=true (a deliberate, reviewable consent in the deployment
config). Without it FeatureLedger stays on the local hashing embedder and template rationales, and
says so once in the log.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("featureledger.llm")
_warned = False


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def external_llm_allowed(env: dict[str, str] | None = None) -> bool:
    global _warned
    env = os.environ if env is None else env
    if not env.get("LLM_API_KEY"):
        return False
    if _truthy(env.get("LLM_ALLOW_EXTERNAL")):
        return True
    if not _warned:
        log.warning("LLM_API_KEY is set but LLM_ALLOW_EXTERNAL is not true: customer text stays local "
                    "(hashing embedder, template rationales). Set LLM_ALLOW_EXTERNAL=true to send it to %s.",
                    env.get("LLM_BASE_URL", "https://api.openai.com/v1"))
        _warned = True
    return False
