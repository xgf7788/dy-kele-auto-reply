"""Listener mixins — split from message_listener.py for maintainability.

Usage in MessageListener:
    from core.listener.page_utils import PageUtilsMixin
    from core.listener.conversation import ConversationMixin
    from core.listener.extraction import ExtractionMixin
    from core.listener.dedup import DedupMixin

    class MessageListener(PageUtilsMixin, ConversationMixin, ExtractionMixin, DedupMixin):
        ...
"""
from .page_utils import PageUtilsMixin
from .conversation import ConversationMixin
from .extraction import ExtractionMixin
from .dedup import DedupMixin

__all__ = [
    "PageUtilsMixin",
    "ConversationMixin",
    "ExtractionMixin",
    "DedupMixin",
]
