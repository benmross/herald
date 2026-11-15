"""Add delivery storage without changing existing replies or topic state."""
from herald import db

DESCRIPTION = "add a durable Telegram reply outbox; preserve all existing topic state"


def apply(con) -> str:
    # db.connect creates new tables on existing installs as well as fresh ones.
    # Keep this additive migration explicit for update and recovery history.
    start = db.SCHEMA.index("CREATE TABLE IF NOT EXISTS telegram_outbox")
    end = db.SCHEMA.index(";", db.SCHEMA.index("CREATE INDEX IF NOT EXISTS idx_telegram_outbox_due", start)) + 1
    for statement in db.SCHEMA[start:end].split(";"):
        if statement.strip():
            con.execute(statement)
    return "Telegram outbox ready; existing messages and sessions unchanged"
