from datetime import datetime, timezone


def utcnow():
    """Naive UTC for the existing database schema."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
