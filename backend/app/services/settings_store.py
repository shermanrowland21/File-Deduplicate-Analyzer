"""
Central app settings — currently the per-function AI model choices, so you can try
different Bedrock models for each analysis task from a Settings UI WITHOUT editing
code or restarting. Persisted to ~/.file_dedup_analyzer/settings.json.

Resolution order for any model setting:
  1. value saved in settings.json (set via the Settings UI)
  2. environment variable (per-function override for ops)
  3. built-in default (cheap-by-default, stays on Bedrock)

Every model choice must be a Bedrock model id / inference profile — nothing here
ever points outside AWS.
"""
import os
import json
import threading

_STORE = os.path.join(os.path.expanduser("~"), ".file_dedup_analyzer", "settings.json")
_lock = threading.Lock()

# key -> (env var name, built-in default). These are the analysis functions whose
# model is user-selectable. Cheap-by-default; all Bedrock.
MODEL_KEYS: dict[str, tuple[str, str]] = {
    "file_analysis":   ("FILE_ANALYSIS_MODEL",  "anthropic.claude-3-5-sonnet-20241022-v2:0"),
    "file_namer_text": ("FILE_NAMER_TEXT_MODEL", "us.deepseek.v3-v1:0"),
    "file_namer_vision": ("FILE_NAMER_VISION_MODEL", "anthropic.claude-3-5-haiku-20241022-v1:0"),
    "folder_namer":    ("FOLDER_NAMER_MODEL",   "us.deepseek.v3-v1:0"),
    "dedup_advisor":   ("DEDUP_ADVISOR_MODEL",  "anthropic.claude-3-5-sonnet-20241022-v2:0"),
    "media_topics":    ("MEDIA_TOPIC_MODEL",    "anthropic.claude-3-5-haiku-20241022-v1:0"),
    "media_visual":    ("MEDIA_VISUAL_MODEL",   "anthropic.claude-3-5-sonnet-20241022-v2:0"),
}

# Human-friendly labels for the Settings UI.
MODEL_LABELS: dict[str, str] = {
    "file_analysis":   "File Analysis (single-file AI analysis)",
    "file_namer_text": "File Renamer — text/documents",
    "file_namer_vision": "File Renamer — images (vision + OCR)",
    "folder_namer":    "Folder Renamer",
    "dedup_advisor":   "Dedup AI Advisor",
    "media_topics":    "Media — topics & summary",
    "media_visual":    "Media — frame/image vision",
}

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_STORE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _cache = {}
    return _cache


def _save(data: dict):
    global _cache
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _STORE)
    _cache = data


def get_model(key: str) -> str:
    """Resolve the model for a function: saved setting → env var → built-in default."""
    env_name, default = MODEL_KEYS.get(key, (None, ""))
    saved = _load().get("models", {}).get(key)
    if saved:
        return saved
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return default


def get_all_models() -> dict:
    """Return every model key with its current effective value + metadata for the
    Settings UI."""
    out = {}
    for key, (env_name, default) in MODEL_KEYS.items():
        out[key] = {
            "label": MODEL_LABELS.get(key, key),
            "value": get_model(key),
            "default": default,
            "env_var": env_name,
            "is_custom": bool(_load().get("models", {}).get(key)),
        }
    return out


def set_model(key: str, model_id: str):
    if key not in MODEL_KEYS:
        raise KeyError(f"unknown model setting: {key}")
    with _lock:
        data = dict(_load())
        models = dict(data.get("models", {}))
        if model_id:
            models[key] = model_id
        else:
            models.pop(key, None)   # empty → revert to env/default
        data["models"] = models
        _save(data)
    return get_model(key)


def reset_model(key: str):
    return set_model(key, "")
