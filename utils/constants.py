"""Shared constants used across the project."""

# ===== Emoji Unicode Pattern =====
# Matches trailing emojis for user name normalization.
# Used in multiple places in message_listener.py — extracted here to avoid duplication.

# Python-compatible version (for re.compile)
EMOJI_PATTERN = (
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "]+$"
)

# JavaScript-compatible version (for embedding in page.evaluate() strings)
# Uses JS \u{XXXXX} syntax. Build as a single string for use in f-strings.
_EMOJI_RANGES_JS = (
    "\\u{1F600}-\\u{1F64F}"
    "\\u{1F300}-\\u{1F5FF}"
    "\\u{1F680}-\\u{1F6FF}"
    "\\u{1F1E0}-\\u{1F1FF}"
    "\\u{2702}-\\u{27B0}"
    "\\u{24C2}-\\u{1F251}"
    "\\u{1F900}-\\u{1F9FF}"
    "\\u{1FA00}-\\u{1FA6F}"
    "\\u{1FA70}-\\u{1FAFF}"
)
EMOJI_PATTERN_JS = _EMOJI_RANGES_JS  # 只用范围，外层 JS 代码会包 [...]+$

# ===== Timing Constants (seconds) =====
# These were previously inline magic numbers scattered throughout the codebase.
PAGE_LOAD_WAIT = 5.0          # Wait for page content to fully render
MESSAGE_RENDER_WAIT = 0.5     # Wait after scroll for lazy-loaded messages
CONVERSATION_SWITCH_WAIT = 2.0 # Base wait after clicking a conversation
LOGIN_CHECK_WAIT = 3.0        # Wait before checking login status
POPUP_CLOSE_WAIT = 1.0        # Wait after closing a popup dialog
STORE_INIT_STAGGER = 2.0      # Delay between initializing each store

# ===== Polling / Loop Constants =====
CONVERSATION_VERIFY_INTERVAL = 0.3   # Check interval during dynamic verification
CONVERSATION_VERIFY_MAX_WAIT = 5.0   # Max wait for conversation verification
UNREAD_DETECT_FAST_POLL = 0.3        # Fast poll interval when processing other user
DEDUP_CLEANUP_INTERVAL = 300         # 5 minutes between LRU dedup cache cleanup
PROCESSING_UNLOCK_TIMEOUT = 300      # Auto-unlock processing user after this many seconds
STATUS_LOG_INTERVAL = 600            # Log heartbeat every N poll iterations (~5 min)

# ===== API / Network Constants =====
API_MAX_RETRIES = 3
API_RETRY_DELAY = 5.0
API_SESSION_POOL_SIZE = 20
API_KEEPALIVE_TIMEOUT = 30

# ===== UI Selector Constants =====
# CSS selectors for the Douyin Kele IM page — centralized for easier maintenance.
SELECTORS_CONVERSATION = [
    '.conversationItem-RaXg9G',
    '[class*="conversationItem"]',
    '[class*="conversation-item"]',
    '[class*="session-item"]',
    '[class*="session-list"] > div',
    '[class*="conversation-list"] > div',
    '[class*="im-session"]',
    '[class*="conversation"]',
    '[class*="session"]',
]

SELECTORS_MESSAGE = [
    '.my-4',
    '.csUI-NormalMessage',
    '.leftMsg-ewM7qC',
    '[class*="leftMsg"]',
    '[class*="message"]',
    '[class*="chat-message"]',
    '[class*="bubble"]',
    '[class*="msg"]',
    '[class*="text"]',
    'div[class*="content"]',
    'div[class*="item"]',
]

SELECTORS_HEADER = [
    '[class*="chat-header"]',
    '[class*="conversation-header"]',
    '[class*="header"] [class*="title"]',
    '[class*="header"]',
    '[class*="title"]',
    '[class*="nickname"]',
    '[class*="user-name"]',
    '[class*="user-info"]',
    '[class*="name"]',
    '[class*="user"]',
    '[class*="cs-header"]',
    '[class*="im-header"]',
    '[class*="session-title"]',
    '[class*="conv-title"]',
]

SELECTORS_INPUT = [
    '[class*="input"] textarea',
    '[class*="message-input"]',
    '[data-e2e="message-input"]',
    'textarea[placeholder*="回复"]',
    'textarea[placeholder*="消息"]',
]

# System status labels that appear in the IM sidebar but are NOT usernames
SYSTEM_STATUS_TEXTS = {'已留资', '未留资', '已回复', '未回复', '广告源', '经营源', '系统消息'}

# Bot message patterns — used to identify messages sent by the bot itself
BOT_MESSAGE_PATTERNS = [
    '您好亲', '小主，', '温馨提示', '服务后可以抵扣',
    '团购券不是全部', '即修到家', '该消息由智能回复',
]

# Stats/system message patterns — these should be skipped (not user messages)
STATS_MESSAGE_PATTERNS = [
    '今日接待数', '留资数', '首响率', '未达标', '用户评分', '商家还在等待你的回复',
]

# Evaluation card patterns — skip these too
EVALUATION_PATTERNS = [
    '评价卡片', '服务评价', '满意度评价', '请对本次服务进行评价',
]
