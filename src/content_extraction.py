"""Separate publication headings from body text without inventing facts."""
import re
from bs4 import BeautifulSoup
from .utils import normalize_text


def paragraph_text(element):
    clone=BeautifulSoup(str(element),'lxml')
    for br in clone.find_all('br'):
        br.replace_with('\n')
    for block in clone.find_all(['p','div','li','h2','h3','h4','blockquote']):
        block.insert_after('\n\n')
    lines=[normalize_text(line) for line in clone.get_text().splitlines()]
    return re.sub(r'\n{3,}','\n\n','\n'.join(lines)).strip()


def telegram_content(element, post_id=''):
    full=paragraph_text(element) if element else ''
    if not full:
        return 'Публикация с медиа','Публикация с медиа без текстового описания.','media',full
    blocks=[b.strip() for b in re.split(r'\n\s*\n',full) if b.strip()]
    if len(blocks)>1 and 4<=len(normalize_text(blocks[0]))<=240 and any(c.isalpha() for c in blocks[0]):
        return normalize_text(blocks[0]),'\n\n'.join(blocks[1:]),'first_paragraph',full
    lines=[line.strip() for line in full.splitlines() if line.strip()]
    if len(lines)>1 and 4<=len(lines[0])<=180 and any(c.isalpha() for c in lines[0]):
        return lines[0],'\n'.join(lines[1:]),'first_line',full
    # A one-paragraph post has no separate publisher heading. Keep its full text.
    return 'Публикация Telegram'+(f' · {post_id}' if post_id else ''),full,'no_separate_heading',full


def remove_repeated_title(text, title):
    text=text.strip()
    if not title:
        return text
    normalized=normalize_text(title)
    lines=text.splitlines()
    if lines and normalize_text(lines[0]).casefold()==normalized.casefold():
        return '\n'.join(lines[1:]).strip()
    return text
