"""
Cosmos v5 — TextRank Summarizer (No-AI)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Algorithm-based extractive summarization.
No GPU, no AI model, pure graph-based ranking.
Quality: ~6/10 — suitable for preview/display.
"""

_summarizer_available = True
try:
    from summa import summarizer as _summa
except ImportError:
    _summarizer_available = False


def summarize(text: str, ratio: float = 0.3, word_count: int = None) -> str:
    """
    Generate an extractive summary using TextRank algorithm.

    Args:
        text: Input text to summarize
        ratio: Fraction of sentences to keep (0.0-1.0)
        word_count: Alternative to ratio — target word count

    Returns:
        Summary string, or original text if too short
    """
    if not text or len(text.strip()) < 200:
        return text.strip() if text else ""

    if not _summarizer_available:
        # Fallback: return first 200 chars
        return text[:200].strip() + "..."

    try:
        if word_count:
            result = _summa.summarize(text, words=word_count)
        else:
            result = _summa.summarize(text, ratio=ratio)

        # summa returns empty string if it can't summarize
        return result.strip() if result else _fallback_summary(text)
    except Exception:
        return _fallback_summary(text)


def _fallback_summary(text: str, max_chars: int = 200) -> str:
    """Simple fallback: first N chars with word boundary."""
    if len(text) <= max_chars:
        return text
    # Find last space before max_chars to avoid cutting words
    cut = text[:max_chars].rfind(' ')
    if cut == -1:
        cut = max_chars
    return text[:cut].strip() + "..."
