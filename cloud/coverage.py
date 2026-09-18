"""Coverage counts only configured sources, including genuine failed checks."""


def monitored(checks):
    # Old imports contain synthetic Telegram rows for companies with no channel.
    # Keep the imported audit records intact; they are not collection attempts.
    return [c for c in checks if not (
        c.get('kind') == 'telegram' and c.get('status') == 'not_configured' and not c.get('url'))]


def outside_scope(checks):
    return len(checks) - len(monitored(checks))
