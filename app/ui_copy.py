"""Compact presentation text; these helpers never change workflow state."""
from tasks import STAGE_ERROR, STAGE_DUPLICATE_REVIEW, STAGE_STOPPED, STAGE_STOPPING


def recovery_label(task, transfer_enabled):
    verb = "Retry" if task.stage == STAGE_ERROR else "Resume"
    if task.output_path:
        return "Continue conversion" if task.convert_in_place or not transfer_enabled else f"{verb} transfer"
    if task.local_input_path or task.convert_in_place:
        return f"{verb} conversion"
    return f"{verb} import"


def row_status_text(task):
    text = task.detail or ""
    if task.stage == STAGE_ERROR:
        if text.startswith("FFmpeg failed:"):
            return "Conversion failed. See Details."
        return text if len(text) <= 180 else text[:177].rstrip() + "…"
    if task.stage in {STAGE_DUPLICATE_REVIEW, STAGE_STOPPED, STAGE_STOPPING} or task.skipped:
        return text if len(text) <= 180 else text[:177].rstrip() + "…"
    # Preserve warnings and unusual waiting conditions, not routine telemetry.
    if any(word in text.lower() for word in ("warning", "not enough", "low disk", "could not", "cannot", "starts in", "waiting for", "verifying", "checking")):
        return text if len(text) <= 180 else text[:177].rstrip() + "…"
    return ""
