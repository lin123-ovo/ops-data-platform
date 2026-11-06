"""Real SSE and history validation using an already built evaluation corpus."""
from __future__ import annotations
import argparse
import json
import re
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--output-name',default='stream_verification.json')
    args=parser.parse_args()
    output=args.report/args.output_name
    if output.exists(): raise SystemExit('Refusing to overwrite verification')
    manifest=json.loads((args.report/'manifest.json').read_text())
    case=json.loads((args.report/'dataset.json').read_text())[0]
    session='graph-stream-'+uuid.uuid4().hex
    base='http://127.0.0.1:8000/api/v1'
    body={'session_id':session,'message':case['question'],'top_k':5,
          'knowledge_base':manifest['knowledge_base'],'retrieval_mode':'graph_hybrid'}
    events=[]
    try:
        request=Request(base+'/chat/stream',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=180) as response:
            raw=response.read().decode()
        for packet in raw.split('\n\n'):
            if not packet.strip(): continue
            lines=packet.splitlines()
            event=next(line[6:].strip() for line in lines if line.startswith('event:'))
            payload=json.loads('\n'.join(line[5:].strip() for line in lines if line.startswith('data:')))
            events.append({'event':event,'data':payload})
        if any(e['event']=='error' for e in events): raise RuntimeError('SSE returned error')
        sources=[e['data'] for e in events if e['event']=='sources']
        final=sources[-1]
        ref_map={item['chunk_id']:item['ref_id'] for item in final['items']}
        stable=all(ref_map.get(item['chunk_id'])==item['ref_id'] for event in sources for item in event['items'])
        answer=''.join(e['data']['text'] for e in events if e['event']=='token')
        refs={item['ref_id'] for item in final['items']}
        cited=[int(n) for n in re.findall(r'\[(\d+)\]',answer)]
        with urlopen(base+'/chat/sessions/'+session+'/history') as response: history=json.load(response)
        checks={'sources_emitted':bool(sources),'graph_ok':final['graph_status']=='ok',
            'has_graph_paths':any(i.get('graph_paths') for i in final['items']),
            'stable_ref_ids':stable,'cited_ids_exist':bool(cited) and set(cited)<=refs,
            'history_preserves_paths':history[0]['source_chunks']==final['items'],
            'history_graph_ok':history[0]['graph_status']=='ok',
            'done_emitted':events[-1]['event']=='done'}
        output.write_text(json.dumps({'checks':checks,'events':events,'history':history},ensure_ascii=False,indent=2),encoding='utf-8')
        if not all(checks.values()): raise RuntimeError('Stream acceptance failed')
        print(json.dumps(checks,ensure_ascii=False,indent=2))
    finally:
        with urlopen(Request(base+'/chat/sessions/'+session,method='DELETE')) as response: response.read()


if __name__=='__main__': main()
