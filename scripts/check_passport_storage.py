"""Private bucket connectivity check. Optional input must be a public passport."""
import argparse,json,sys,tomllib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from price_monitor.passport_transport import SupabaseFiles

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--settings',default='.streamlit/secrets.toml')
    parser.add_argument('--public-pdf',type=Path);args=parser.parse_args()
    config=tomllib.loads(Path(args.settings).read_text(encoding='utf-8-sig'))
    storage=SupabaseFiles(config.get('passports',{}));storage.ensure()
    result={'private_bucket':True}
    if args.public_pdf:
        from price_monitor.passport_recognition import inspect_pdf
        import requests
        raw=args.public_pdf.read_bytes();inspect_pdf(raw)
        fp,key=storage.put(raw)
        result['fingerprint']=fp;result['read_verified']=storage.get(fp)==raw
        result['duplicate_reused']=storage.put(raw)[0]==fp
        signed=storage.signed_url(fp)
        response=requests.get(signed,timeout=45,allow_redirects=False)
        result['signed_view_verified']=response.status_code==200 and response.content==raw
        public=storage.url+'/storage/v1/object/public/'+storage.bucket+'/'+key
        response=requests.get(public,timeout=25,allow_redirects=False)
        result['anonymous_denied']=response.status_code in (400,401,403,404)
        if not all(result[k] for k in ('read_verified','duplicate_reused','signed_view_verified','anonymous_denied')):
            raise RuntimeError('Storage verification failed')
    print(json.dumps(result),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Provider responses, settings and signed URLs never enter console logs.
        safe=str(exc) if isinstance(exc,(RuntimeError,ValueError)) and str(exc).startswith(('Хранилище паспортов: HTTP ','Хранилище паспортов должно','Укажите HTTPS URL','Не задан passports.','Сохранение паспорта: HTTP ','Ссылка на паспорт: HTTP ')) else type(exc).__name__
        print('Storage check failed: '+safe,flush=True)
        raise SystemExit(1)
