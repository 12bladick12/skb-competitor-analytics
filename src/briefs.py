"""Short extractive briefs with optional offline translation and safe fallback."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from bs4 import BeautifulSoup

from .utils import normalize_text


def article_body(soup, selectors=(), title=''):
    from .content_extraction import paragraph_text, remove_repeated_title

    def extract(selector):
        pieces=[]
        elements=soup.select(selector)
        for element in elements:
            if any(parent in elements for parent in element.parents):
                continue
            clone=BeautifulSoup(str(element),'lxml')
            for noise in clone.select('header,footer,nav,aside,form,script,style,h1,time,.breadcrumbs,.breadcrumb,.related-posts,.related-news,.share,.social,.pagination,.news-date,.post-meta,.entry-meta,.newsletter,.toc,.ez-toc-container'):
                noise.decompose()
            blocks=clone.select('p,li,h2,h3,h4,blockquote')
            blocks=[block for block in blocks if not any(parent in blocks for parent in block.parents)]
            texts=[paragraph_text(block) for block in blocks] if blocks else [paragraph_text(clone)]
            texts=[remove_repeated_title(text,title) for text in texts]
            texts=[text for text in texts if text and not re.search(r'cookie|copyright|все права защищены|политик[аи] конфиденциальности|subscribe to our newsletter',text,re.I)]
            pieces.extend(texts)
        return '\n\n'.join(pieces).strip()

    for selector in selectors:
        text=extract(selector)
        if len(text)>=40:
            return text

    def walk(value):
        if isinstance(value,dict):
            yield value
            for nested in value.values():
                yield from walk(nested)
        elif isinstance(value,list):
            for nested in value:
                yield from walk(nested)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(script.get_text())
            candidates=[entry for entry in walk(obj) if isinstance(entry.get('articleBody'),str)]
            candidates=[entry for entry in candidates if not title or not entry.get('headline') or normalize_text(entry['headline']).casefold()==normalize_text(title).casefold()]
            if len(candidates)==1:
                text=remove_repeated_title(paragraph_text(BeautifulSoup(candidates[0]['articleBody'],'lxml')),title)
                if len(text)>=40:
                    return text
        except (ValueError, TypeError):
            pass
    for selector in ['.news-detail', '.news_detail', '.entry-content', '.article-content',
                     '.article-main', '.news-detail-content', '.news-details', '.news_content',
                     '.post-content', '.fl-rich-text', '.article-body', '[itemprop="articleBody"]', 'article', 'main', '#content']:
        text=extract(selector)
        if len(text)>=40:
            return text
    return ''


def brief(text, limit=420):
    text = normalize_text(text)
    text = re.sub(r'\[View[^\]]*\]', '', text, flags=re.I)
    text = re.sub(r'^Detail\s+', '', text)
    text = re.sub(r'^[^\w«“]+', '', text)
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-ZА-ЯЁ«“])', text)
    selected = []
    for sentence in sentences:
        if re.search(r'cookie|skip to content|all rights reserved|privacy policy', sentence, re.I):
            continue
        if len(' '.join(selected + [sentence])) > limit and selected:
            break
        selected.append(sentence)
        if len(selected) == 2:
            break
    result = ' '.join(selected) or text
    if len(result) > limit:
        result = result[:limit].rsplit(' ', 1)[0] + '…'
    return result


def protected_tokens(text):
    models=set(re.findall(r'\b[A-ZА-Я]{1,}[A-ZА-Я0-9-]*\d[A-ZА-Я0-9-]*\b|\b[A-Z]{2,}[A-Z0-9-]*\b',text))
    numbers={n.replace(',','.').replace(' ','') for n in re.findall(r'\d+(?:[.,]\d+)?(?:\s*[%°])?',text)}
    units=set(re.findall(r'\b(?:mm|cm|MHz|kHz|GHz|MPa|kPa|mA|mV|kW|VDC|VAC)\b',text))
    return models|numbers|units


def terminology_preserved(original, translated):
    terms = {'capacitive':r'ёмкостн|емкостн', 'inductive':r'индуктивн',
             'photoelectric':r'фотоэлектр|оптическ', 'proximity':r'приближен|бесконтакт',
             'power controller':r'регулятор.{0,20}мощност|контроллер.{0,20}мощност',
             'encoder':r'энкодер|преобразователь.{0,20}перемещен'}
    return all(re.search(expected,translated,re.I) for term,expected in terms.items() if term in original.casefold())


def translation_environment():
    root=Path(__file__).resolve().parents[1]/'data'/'translation'
    for variable,folder in [('XDG_DATA_HOME','data'),('XDG_CACHE_HOME','cache'),('XDG_CONFIG_HOME','config')]:
        os.environ[variable]=str(root/folder)
    packages=root/'packages'
    packages.mkdir(parents=True,exist_ok=True)
    package_path=str(packages)
    # SentencePiece on Windows opens a narrow filename. Use the filesystem's
    # ASCII alias without moving models out of the project directory.
    if os.name=='nt':
        import ctypes
        buffer=ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(package_path,buffer,len(buffer)) and buffer.value.isascii():
            package_path=buffer.value
    os.environ['ARGOS_PACKAGES_DIR']=package_path
    os.environ['ARGOS_DEVICE_TYPE']='cpu'
    os.environ['ARGOS_INTRA_THREADS']='2'


@lru_cache(maxsize=4096)
def russian_brief(text, enabled=True):
    original = brief(text)
    if len(re.findall('[А-Яа-яЁё]', original)) > len(re.findall('[A-Za-z]', original)) / 3:
        return original
    if enabled:
        try:
            translation_environment()
            import argostranslate.translate
            source = 'de' if re.search(r'\b(und|der|die|das|für|mit|von)\b', original) else 'en'
            translated = argostranslate.translate.translate(original, source, 'ru')
            if (re.search('[А-Яа-яЁё]', translated)
                    and protected_tokens(original) == protected_tokens(translated)
                    and terminology_preserved(original,translated)
                    and len(translated) < len(original) * 4):
                return translated
        except Exception:
            pass
    return '[Оригинал; перевод недоступен или не прошёл проверку] ' + original
