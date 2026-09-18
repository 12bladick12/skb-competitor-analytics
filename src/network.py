"""One transport policy for requests, Chromium and Telegram."""
from __future__ import annotations

import os
from urllib.parse import urlparse


def system_proxy() -> str:
    # Read Windows explicitly: managed-shell environment proxies are unrelated
    # to the user's Internet Settings and must not silently win over them.
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
                if winreg.QueryValueEx(key, 'ProxyEnable')[0]:
                    value = str(winreg.QueryValueEx(key, 'ProxyServer')[0])
                    if '=' in value:
                        parts = dict(p.split('=', 1) for p in value.split(';') if '=' in p)
                        value = parts.get('https') or parts.get('http') or ''
                    return value if '://' in value else ('http://' + value if value else '')
        except OSError:
            pass
        return ''
    return os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or ''


def resolved_proxy(settings) -> str:
    mode = getattr(settings, 'proxy_mode', 'legacy')
    if mode == 'direct':
        return ''
    if mode == 'system':
        return system_proxy()
    if mode == 'explicit':
        return settings.proxy_url
    return settings.proxy_url or system_proxy() if settings.use_system_proxy else ''


def telegram_proxy(settings):
    value = resolved_proxy(settings)
    parsed = urlparse(value)
    if not parsed.hostname:
        return None
    scheme = 'http' if parsed.scheme == 'https' else parsed.scheme
    return (scheme, parsed.hostname, parsed.port or 8080, True, parsed.username, parsed.password)
