"""Build a scoped release on the exact published commit, without local secrets/data."""
from pathlib import Path
import argparse
import difflib
import io
import json
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'analysis' / 'search_release_2026_10_01'
FILES = [
    'price_monitor/catalog_search.py', 'price_monitor/catalog_search_ui.py',
    'price_monitor/comparison_groups.py', 'price_monitor/group_comparison_ui.py',
    'price_monitor/automatic_price_terms.py', 'price_monitor/own_prices.py',
    'price_monitor/price_terms.py', 'price_monitor/storage.py',
    'price_monitor/matching_ui.py', 'price_monitor/library.py', 'price_monitor/details.py', 'price_monitor/adapters.py',
    'tests_prices/test_catalog_search.py', 'tests_prices/test_group_comparison.py',
    'tests_prices/test_comparison_catalog.py', 'tests_prices/test_comparison_catalog_pg.py',
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    if not len(args.base) == 40 or any(c not in '0123456789abcdef' for c in args.base):
        raise ValueError('Expected a pinned commit SHA')
    OUT.mkdir(parents=True, exist_ok=True)
    archive = OUT / 'base.zip'
    if not archive.exists():
        url = f'https://codeload.github.com/12bladick12/skb-competitor-analytics/zip/{args.base}'
        with urllib.request.urlopen(url, timeout=60) as response:
            archive.write_bytes(response.read())
    candidate = OUT / 'candidate'
    baseline = OUT / 'base'
    for destination in (baseline, candidate):
        destination.mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as zipped:
            for info in zipped.infolist():
                relative = Path(*Path(info.filename).parts[1:])
                if not relative.parts or info.is_dir():
                    continue
                target = (destination / relative).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError('Unexpected archive path')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zipped.read(info))
    changed = []
    patches = []
    for name in FILES:
        old_path, source = baseline / name, ROOT / name
        old = old_path.read_text(encoding='utf-8-sig') if old_path.exists() else ''
        new = source.read_text(encoding='utf-8-sig')
        if old == new:
            continue
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding='utf-8', newline='\n')
        changed.append({'path': name, 'mode': '100644', 'type': 'blob', 'content': new})
        patches.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile='a/'+name, tofile='b/'+name))
    (OUT / 'changes.json').write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
    (OUT / 'release.diff').write_text(''.join(patches), encoding='utf-8')
    (OUT / 'metadata.json').write_text(json.dumps({'base': args.base, 'repository': '12bladick12/skb-competitor-analytics',
        'files': [r['path'] for r in changed]}, indent=2), encoding='utf-8')
    print(json.dumps({'base': args.base, 'files': [{'path': r['path'], 'bytes': len(r['content'].encode('utf-8'))} for r in changed]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
