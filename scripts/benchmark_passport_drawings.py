"""Local-only benchmark on explicitly supplied internal reference documents.

Inputs and outputs stay in ignored data/. Nothing is uploaded or approved.
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from price_monitor.passport_recognition import LocalRecognizer,pdf_module,ocr_png


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--limit',type=int,default=3);args=parser.parse_args()
    index=Path('analysis/capacitive_research_2026_09_30/provided_passports/index.json')
    controls=json.loads(index.read_text(encoding='utf-8'))
    root=Path('data/passport_benchmark');root.mkdir(exist_ok=True)
    fitz=pdf_module();results=[]
    for row in [controls[i] for i in (0,1,9)][:args.limit]:
        started=time.monotonic();raw=Path(row['source']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('Контрольный паспорт изменился')
        original_page=row['drawing_pages'][0]
        with fitz.open(stream=raw,filetype='pdf') as source,fitz.open() as subset:
            subset.insert_pdf(source,from_page=original_page-1,to_page=original_page-1)
            payload=subset.tobytes()
            page=subset[0]
            png=page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False).tobytes('png')
        (root/f'control_{row["index"]}.pdf').write_bytes(payload)
        text=ocr_png(png)
        (root/f'control_{row["index"]}.ocr.txt').write_text(text,encoding='utf-8')
        result={'index':row['index'],'original_fingerprint':row['sha256'],'original_page':original_page,
                'preparation_seconds':round(time.monotonic()-started,2),'ocr_characters':len(text),
                'approved':False}
        if not args.prepare_only:
            started=time.monotonic()
            try:result['proposal']=LocalRecognizer()(payload)
            except Exception as exc:result['error']=type(exc).__name__+': '+str(exc)[:300]
            result['recognition_seconds']=round(time.monotonic()-started,2)
        results.append(result)
        (root/('preparation.json' if args.prepare_only else 'results.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='proposal'},ensure_ascii=True),flush=True)


if __name__=='__main__':main()
