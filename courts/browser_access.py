"""Navigation resilience without changing browser identity or access checks."""
import re
from urllib.parse import urlsplit

# Only optional analytics/social/support widgets. Court scripts, styles, data,
# authentication and challenge resources continue to load normally.
OPTIONAL_HOSTS = frozenset({
    'www.googletagmanager.com', 'www.google-analytics.com',
    'region1.google-analytics.com', 'region1.analytics.google.com',
    'mc.yandex.ru', 'mc.yandex.md', 'top-fwz1.mail.ru', 'privacy-cs.mail.ru',
    'stat.sputnik.ru', 'supportkad.bot.one',
})


def optional_resource(url):
    parsed = urlsplit(url)
    return parsed.hostname in OPTIONAL_HOSTS or (
        parsed.hostname == 'vk.com' and parsed.path == '/js/api/openapi.js')


def route_optional(route):
    if route.request.resource_type != 'document' and optional_resource(route.request.url):
        route.abort()
    else:
        route.continue_()


def error_code(exc):
    """Stable diagnostics only; exceptions can contain URLs, cookies or tokens."""
    match = re.search(r'net::(ERR_[A-Z0-9_]+)', str(exc))
    if match:return match[1]
    message=str(exc).lower()
    for needle,code in [('strict mode violation','LOCATOR_AMBIGUOUS'),
                        ('execution context was destroyed','NAVIGATION_CHANGED'),
                        ('page is navigating','NAVIGATION_CHANGED'),
                        ('cannot read properties of null','DOM_NOT_READY')]:
        if needle in message:return code
    return type(exc).__name__


def navigate(page, url):
    # DOMContentLoaded can be held up by a third-party script even though the
    # form is already present. Individual adapters wait for their own controls.
    response = page.goto(url, wait_until='commit', timeout=40000)
    page.locator('body').wait_for(state='attached', timeout=10000)
    return response


def wait_for_search_end(page, marker='Идет поиск'):
    page.wait_for_function('marker => document.body && !document.body.innerText.includes(marker)',
                           arg=marker,timeout=35000)


def wait_for_react_input(page, placeholder):
    # SSR renders an editable field before React attaches its event handlers.
    # Filling it sooner loses the value on hydration and submits an empty search.
    page.wait_for_function('''placeholder => {
        const input=Array.from(document.querySelectorAll('input')).find(
            e=>e.getAttribute('placeholder')===placeholder);
        return input && Object.keys(input).some(k=>k.startsWith('__reactProps'));
    }''',arg=placeholder,timeout=25000)


def capture(page, result):
    import hashlib
    html = page.content().encode()
    proof = hashlib.sha256(html).hexdigest()
    result.objects[proof] = html
    result.url = page.url
    return proof
