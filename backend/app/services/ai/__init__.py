"""AI-assisted authoring: provider abstraction, item generation, CME authoring.

Nothing in this package can publish. Everything it produces lands at
``EditorialStatus.AI_DRAFT`` and needs a named human to move it further —
see ``app.services.ai.pipeline`` and ``app.services.editorial``.
"""

from app.services.ai.provider import (
    AiProvider,
    AiUnavailable,
    MockProvider,
    ProviderResponse,
    Usage,
    get_provider,
)

__all__ = [
    "AiProvider",
    "AiUnavailable",
    "MockProvider",
    "ProviderResponse",
    "Usage",
    "get_provider",
]
