"""Keep private capabilities within the public collector's existing categories."""
from toolsets import BUILTIN_TOOL_NAMES as _REGISTERED_TOOL_NAMES
from toolsets import BUILTIN_TOOLSET_NAMES as _REGISTERED_TOOLSET_NAMES

# Runtime registration remains intact; only metrics use plugin/custom buckets.
BUILTIN_TOOL_NAMES = _REGISTERED_TOOL_NAMES - {"voice_assistant"}
BUILTIN_TOOLSET_NAMES = _REGISTERED_TOOLSET_NAMES - {"live_voice"}
