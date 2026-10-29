"""Keep earlier opportunity reasoning when replacing the default long view."""

from herald import context

DESCRIPTION = "add retrieval measurements and archive the old opportunity overview before compacting"


def apply(con) -> str:
    archive = context.refresh_opportunities(con)
    return ('saved prior opportunity overview to ' + archive.relative_to(context.config.LEDGER).as_posix()
            if archive else 'compact opportunity overview refreshed; prior archives retained')
