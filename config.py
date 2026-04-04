from settings import settings

STT_URL = settings.stt_url
LLM_CHAIN = settings.llm_chain
TTS_URL = settings.tts.url
TTS_MODEL = settings.tts.model
TTS_VOICE = settings.tts.voice
TTS_FORMAT = settings.tts.format
TTS_VOICES = settings.tts.voices
TTS_FALLBACK_URL = settings.tts.fallback_url
TTS_FALLBACK_MODEL = settings.tts.fallback_model
TTS_FALLBACK_VOICE = settings.tts.fallback_voice
AGENT_PORT = settings.agent_port
DOWNLOADS_DIR = settings.downloads_dir
READER_DIR = settings.reader.directory
MAX_TOOL_ROUNDS = settings.max_tool_rounds
MAX_CONVERSATION_MESSAGES = settings.max_conversation_messages
TOOL_LABELS = settings.tool_labels
MCP_SERVERS = settings.mcp_servers
SYSTEM_PROMPT = settings.system_prompt
