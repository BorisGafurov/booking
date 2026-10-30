VERSION = "006_remove_guessed_placeholders"
NAME = "Historical placeholder cleanup (no-op)"


def apply(conn) -> None:
    """Legacy placeholder rows were already removed on existing installations.

    New installations never create them, so this migration intentionally does
    nothing. It is kept to preserve the migration chain.
    """


def rollback(conn) -> None:
    pass
