"""Preserve existing sessions while adding a journal for unfinished turns."""
from herald import db

DESCRIPTION = 'add durable Telegram requests and early native-session checkpoints'


def apply(con) -> str:
    start = db.SCHEMA.index('CREATE TABLE IF NOT EXISTS telegram_turns')
    end = db.SCHEMA.index(';', db.SCHEMA.index('CREATE INDEX IF NOT EXISTS idx_telegram_turns_pending', start)) + 1
    for statement in db.SCHEMA[start:end].split(';'):
        if statement.strip():
            con.execute(statement)
    return 'Turn journal ready; existing topic state and transcripts preserved'
