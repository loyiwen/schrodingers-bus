"""Batch handling for Schrödinger's Bus, kept free of Azure so it can be tested alone."""
import json
import re

# Starts with a letter or digit, then up to 127 letters, digits, hyphens or underscores
BATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def is_valid_batch_id(batch_id):
    """Batch IDs become file names, so only allow a safe set of characters."""
    # Also rules out slashes and dots, which could reach other files or folders
    return bool(batch_id) and BATCH_ID.fullmatch(batch_id) is not None


def parse_batch(text):
    """Return the polls in a JSON Lines batch, or raise ValueError if it's malformed."""
    polls = []
    # Each non-blank line is one poll, numbered from 1 for error messages
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            poll = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"line {number} is not valid JSON") from error
        # Only check the fields every later step relies on
        if not isinstance(poll, dict) or "polled_at" not in poll or "ok" not in poll or not isinstance(poll.get("arrivals"), list):
            raise ValueError(f"line {number} needs polled_at, ok and a list of arrivals")
        polls.append(poll)
    if not polls:
        raise ValueError("batch is empty")
    return polls