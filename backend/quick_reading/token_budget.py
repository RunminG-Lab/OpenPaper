from __future__ import annotations

import math


# These are compatibility estimates, never reported as a model's official limits.
DEFAULT_CONTEXT_LIMIT = 32768
DEFAULT_OUTPUT_LIMIT = 8192
SYSTEM_TOKEN_RESERVE = 256
IMAGE_TOKEN_ESTIMATE = 4096


def positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def estimate_text_tokens(text):
    """Conservative mixed Chinese/English estimate without a provider tokenizer."""
    text = text or ""
    ascii_count = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_count / 3 + (len(text) - ascii_count) * 2)


def plan_token_budget(prompt, image_count, depth, api_config, capabilities=None, figure_count=None):
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    api_config = api_config if isinstance(api_config, dict) else {}
    official_context = positive_int(capabilities.get("context_window"))
    official_output = positive_int(capabilities.get("max_output_tokens"))
    manual_context = positive_int(api_config.get("contextWindow"))
    manual_output = positive_int(api_config.get("maxOutputTokens"))

    context_limit = official_context or manual_context or DEFAULT_CONTEXT_LIMIT
    if manual_context:
        context_limit = min(context_limit, manual_context)
    output_limit = official_output or manual_output or DEFAULT_OUTPUT_LIMIT
    figure_count = image_count if figure_count is None else figure_count
    requested_output = manual_output or ({"brief": 8192, "standard": 16384, "deep": 24576}.get(depth, 16384) + figure_count * 768)
    target_output = min(requested_output, output_limit)
    if not manual_output:
        # Leave room for paper evidence even on smaller-context models.
        target_output = min(target_output, max(1, context_limit // 2))

    estimated_input = estimate_text_tokens(prompt) + SYSTEM_TOKEN_RESERVE + image_count * (IMAGE_TOKEN_ESTIMATE + 128)
    safety_tokens = max(1024, min(4096, context_limit // 50))
    available_output = max(0, context_limit - estimated_input - safety_tokens)
    max_output = min(target_output, available_output)
    sources = capabilities.get("sources")
    official_sources = [source for source in sources if isinstance(source, str)] if isinstance(sources, list) else []
    source_parts = []
    if official_context or official_output:
        source_parts.append("official")
    if manual_context or manual_output:
        source_parts.append("manual")
    if (not official_context and not manual_context) or (not official_output and not manual_output):
        source_parts.append("conservative_default")
    notes = ["输入 token 按完整提示词、系统消息及实际图片保守估算；未使用供应商精确分词器。"]
    if "conservative_default" in source_parts:
        notes.append("部分官方上限未知，使用兼容估算；可在设置中填写已确认的上下文或输出上限。")
    if manual_context and official_context and manual_context > official_context:
        notes.append("手动上下文已限制为官方上下文上限。")
    if manual_output and official_output and manual_output > official_output:
        notes.append("手动输出已限制为官方最大输出上限。")
    if max_output < target_output:
        notes.append("输出预算受剩余上下文限制。")
    return {
        "estimated_input_tokens": estimated_input,
        "estimated_image_tokens": image_count * IMAGE_TOKEN_ESTIMATE,
        "image_count": image_count,
        "figure_count": figure_count,
        "max_output_tokens": max_output,
        "requested_output_tokens": requested_output,
        "target_output_tokens": target_output,
        "context_window": official_context,
        "model_max_output_tokens": official_output,
        "effective_context_limit": context_limit,
        "effective_output_limit": output_limit,
        "safety_tokens": safety_tokens,
        "limits_source": "+".join(source_parts),
        "limit_sources": official_sources,
        "budget_notes": notes,
        "target_fits": max_output == target_output,
    }
