"""Shared string constants that must change together across the project.

Bare literals like the ``(untitled)`` display fallback and the claude.ai
chat-URL prefix were independently re-typed at many call sites; collecting them
here gives each one home so a change can't half-land. Import-free.
"""

# Display name shown when an item has no extractable title. The search/index
# paths and the viewer must agree on this exact spelling: the search name bonus
# keys off the RAW (pre-substitution) name, so a stored "(untitled)" stays
# distinct from a conversation literally titled "(untitled)".
UNTITLED = "(untitled)"

# The claude.ai conversation URL scheme — used to BUILD a thread link
# (providers.provider_url) and to PARSE one back to a bare id (normalize_uuid).
CLAUDE_CHAT_URL_PREFIX = "https://claude.ai/chat/"
