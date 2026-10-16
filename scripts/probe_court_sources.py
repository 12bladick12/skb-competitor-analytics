"""Run the same public adapters without database/Drive writes or release claims."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from courts.registry import entities
from courts.sources import PublicBrowser, CATALOG


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--sources',nargs='+',choices=CATALOG,default=['kad','vsrf','fssp','sudact_arbitral'])
    parser.add_argument('--entity',default='7453019186')
    parser.add_argument('--channel',choices=['msedge','chrome'])
    parser.add_argument('--pages',type=int,default=1)
    args=parser.parse_args();registry=entities()
    selected=registry if args.entity=='all' else [next(e for e in registry if e['id']==args.entity)]
    args.output.mkdir(parents=True,exist_ok=True)
    results=[]
    with PublicBrowser(channel=args.channel) as browser:
      for entity in selected:
        for source in args.sources:
            cursor=None
            for number in range(max(1,min(args.pages,30))):
                result=browser.fetch(source,entity,cursor);result.validate()
                metadata=asdict(result);objects=metadata.pop('objects')
                metadata['evidence_ids']=list(objects)
                for key,content in objects.items():(args.output/(key+'.html')).write_bytes(content)
                results.append(metadata)
                (args.output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'source':source,'entity_id':entity['id'],'page':number+1,'status':result.status,
                                  'cases':len(result.cases),'candidates':len(result.candidates),'reason':result.reason,
                                  'diagnostics':result.diagnostics},ensure_ascii=False),flush=True)
                cursor=result.next_cursor
                if cursor is None or result.status in ('error','challenge','access_denied','rate_limited'):break


if __name__=='__main__':main()
