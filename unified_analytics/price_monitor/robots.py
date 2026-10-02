"""Robots rules with * / $ and longest-match Allow precedence (RFC 9309)."""
from dataclasses import dataclass, field
import re
from urllib.parse import quote, urlsplit


def comparable(value):
    # Match encoded Cyrillic paths while preserving reserved percent escapes.
    value = quote(value, safe="/%*?$=&:+;,@!~'()#[]-._")
    return re.sub(r"%[0-9a-fA-F]{2}", lambda m: chr(int(m[0][1:], 16)) if chr(int(m[0][1:], 16)) in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~" else m[0].upper(), value)


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)
    delay: float = 0


class Robots:
    def __init__(self, text: str, agent: str = "PriceMonitor"):
        groups, current, seen_rule = [], Group(), False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if seen_rule:
                    groups.append(current)
                    current, seen_rule = Group(), False
                current.agents.append(value.lower())
            elif current.agents and key in {"allow", "disallow", "crawl-delay"}:
                seen_rule = True
                if key == "crawl-delay":
                    try:
                        current.delay = max(0, float(value))
                    except ValueError:
                        pass
                elif value:
                    current.rules.append((key, value))
        groups.append(current)
        matches = [(max([len(a) for a in g.agents if a != "*" and a in agent.lower()] or [0]), g) for g in groups]
        best = max((m for m, _ in matches), default=0)
        selected = [g for m,g in matches if (m == best if best else "*" in g.agents)]
        self.rules = [r for g in selected for r in g.rules]
        self.delay = max([g.delay for g in selected] or [0])

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        path = comparable((parts.path or "/") + ("?" + parts.query if parts.query else ""))
        matches = []
        for directive, pattern in self.rules:
            pattern = comparable(pattern)
            end = pattern.endswith("$")
            body = pattern[:-1] if end else pattern
            regex = "^" + ".*".join(re.escape(p) for p in body.split("*")) + ("$" if end else "")
            if re.search(regex, path):
                matches.append((len(body.replace("*", "").encode()), directive == "allow"))
        return max(matches)[1] if matches else True
