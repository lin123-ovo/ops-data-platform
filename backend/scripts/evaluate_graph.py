"""Live, isolated synthetic Graph RAG comparison. Labels are saved before retrieval.

Creates 20 tiny documents ONLY in a new graph-eval-* knowledge base. Never rebuilds
existing documents or overwrites historical benchmarks. --chat adds real Qwen calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


def api(base, route, payload=None, method=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = Request(base+route, data=data, headers={'Content-Type': 'application/json'}, method=method)
    with urlopen(request, timeout=180) as response:
        return json.load(response)


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values)-1, int((len(values)-1)*p))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--chat', action='store_true')
    parser.add_argument('--reuse-corpus', type=Path)
    parser.add_argument('--base', default='http://127.0.0.1:8000/api/v1')
    parser.add_argument('--baseline-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.run_live:
        raise SystemExit('Use --run-live to explicitly create an isolated corpus and call configured models.')
    args.output.mkdir(parents=True, exist_ok=False)
    def save(name, value):
        (args.output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.reuse_corpus:
        fixtures = json.loads((args.reuse_corpus/'corpus.json').read_text())
        cases = json.loads((args.reuse_corpus/'dataset.json').read_text())
        manifest = json.loads((args.reuse_corpus/'manifest.json').read_text())
        kb, doc_ids = manifest['knowledge_base'], manifest['document_ids']
        for name in ('corpus.json', 'dataset.json', 'manifest.json'):
            save(name, json.loads((args.reuse_corpus/name).read_text()))
    else:
        kb = 'graph-eval-'+uuid.uuid4().hex[:10]
        fixtures, cases, doc_ids = [], [], []
        # Distinct opaque component names make the cross-document link explicit, not guessed from semantics.
        names = ['霁云','青岚','映川','星澜','照月','怀山','闻溪','临风','远舟','栖鹭']
        components = ['澄明','砺石','清越','凝辉','沧溟','竹影','松涛','寒露','微澜','白榆']
        for i, (name, component) in enumerate(zip(names, components), 1):
            system, comp, rule = name+'系统', component+'组件', '约束准则'+str(i)
            source_a = f'{system}依赖{comp}。该系统的服务目录由运维团队维护。'
            source_b = f'{rule}约束{comp}。该规则要求每{10+i}天执行一次完整性校验。'
            evidence_ids = []
            for role, content in [('架构',source_a), ('准则',source_b)]:
                doc = api(args.base, '/documents/ingest-text', {'filename':f'{name}-{role}.txt', 'content':content, 'knowledge_base':kb})['document']
                doc_ids.append(doc['id'])
                chunks = api(args.base, '/documents/'+doc['id']+'/chunks')
                if len(chunks) != 1:
                    raise RuntimeError('Synthetic fixture should produce exactly one chunk')
                evidence_ids.append(chunks[0]['id'])
                fixtures.append({'document_id':doc['id'], 'chunk_id':chunks[0]['id'], 'content':content})
            for variant, question in enumerate([
                f'{system}依赖的组件受哪条规则约束？校验周期是多少天？',
                f'沿{system}的组件依赖关系，找出适用的完整性校验规则和要求。',
            ]):
                cases.append({'id':f'G{i:02}-{variant+1}', 'question':question,
                    'relevant_chunk_ids':evidence_ids, 'expected_terms':[comp,rule,str(10+i)], 'category':'cross_document_two_hop'})
        save('corpus.json', fixtures); save('dataset.json', cases)
        save('manifest.json', {'knowledge_base':kb, 'document_ids':doc_ids,
            'corpus_sha256': hashlib.sha256(json.dumps(fixtures, sort_keys=True).encode()).hexdigest(),
            'note':'Synthetic, model-authored cases; no claim of production accuracy. Labels saved before any retrieval. One sequential pass; no load test.'})
    submitted = api(args.base, '/graph/build', {'document_ids':doc_ids})
    target_jobs = {j['id'] for j in submitted['items']}
    deadline = time.monotonic()+900
    while time.monotonic() < deadline:
        tasks = [t for t in api(args.base, '/graph/tasks?knowledge_base='+kb+'&limit=100')['items'] if t['id'] in target_jobs]
        if len(tasks) == len(doc_ids) and all(t['status']=='succeeded' for t in tasks):
            save('build_tasks.json', tasks)
            break
        if any(t['status']=='failed' for t in tasks):
            save('build_tasks.json', tasks)
            raise RuntimeError('Graph extraction failed; inspect saved task errors')
        print('build', sum(t['status']=='succeeded' for t in tasks), '/', len(doc_ids), flush=True)
        time.sleep(5)
    else:
        raise TimeoutError('Graph build deadline exceeded')
    results, chats = [], []
    for mode in ['hybrid', 'graph_hybrid']:
        for case in cases:
            started = time.perf_counter()
            response = api(args.base, '/retrieval/search', {'query':case['question'], 'top_k':5,
                'knowledge_base':kb, 'retrieval_mode':mode})
            ids = {h['chunk_id'] for h in response['items']}
            relevant = set(case['relevant_chunk_ids'])
            results.append({'id':case['id'], 'mode':mode, 'recall':len(ids & relevant)/len(relevant),
                'complete_evidence':relevant <= ids, 'latency_ms':1000*(time.perf_counter()-started), **response})
            save('retrieval_results.json', results)
            if args.chat:
                chat = api(args.base, '/chat', {'session_id':'graph-eval-'+uuid.uuid4().hex,
                    'message':case['question'], 'top_k':5, 'knowledge_base':kb, 'retrieval_mode':mode})
                refs = {h['ref_id']:h['chunk_id'] for h in chat['source_chunks']}
                cited = [int(r) for r in re.findall(r'\[(\d+)\]', chat['answer'])]
                # A conservative fixture-based proxy; keep raw answers for human review.
                chats.append({'id':case['id'], 'mode':mode, 'cites_only_relevant': bool(cited) and all(refs.get(r) in relevant for r in cited),
                    'expected_terms_present':all(term in chat['answer'] for term in case['expected_terms']), **chat})
                save('chat_results.json', chats)
                api(args.base, '/chat/sessions/'+chat['session_id'], method='DELETE')
        print('evaluated', mode, flush=True)
    summary = {}
    for mode in ['hybrid', 'graph_hybrid']:
        rows = [r for r in results if r['mode']==mode]
        summary[mode] = {'recall_at_5':statistics.mean(r['recall'] for r in rows),
            'complete_evidence_rate':statistics.mean(r['complete_evidence'] for r in rows),
            'p95_ms':percentile([r['latency_ms'] for r in rows], .95)}
        answers = [r for r in chats if r['mode']==mode]
        if answers:
            summary[mode].update(citation_proxy=statistics.mean(r['cites_only_relevant'] for r in answers),
                                answer_term_proxy=statistics.mean(r['expected_terms_present'] for r in answers))
    baseline_results = []
    if args.baseline_dir:
        # Check original labels against current corpus before reusing them.
        old = json.loads((args.baseline_dir/'dataset.json').read_text())
        corpus = json.loads((args.baseline_dir/'corpus.json').read_text())
        did = corpus[0]['document_id']
        current = api(args.base, '/documents/'+did+'/chunks')
        signature = lambda rows: sorted((r['id'],r['content']) for r in rows)
        if signature(current) != signature(corpus):
            raise RuntimeError('Original benchmark corpus changed; labels cannot be reused')
        for mode in ['hybrid','graph_hybrid']:
            for case in old:
                response = api(args.base, '/retrieval/search', {'query':case['question'], 'top_k':5,
                    'retrieval_mode':mode, 'knowledge_base':current[0]['metadata_json'].get('knowledge_base','default')})
                relevant=set(case['relevant_chunk_ids']); ids={h['chunk_id'] for h in response['items']}
                baseline_results.append({'id':case['id'], 'mode':mode, 'recall':len(ids & relevant)/len(relevant)})
        save('original_benchmark.json', baseline_results)
    improved = summary['graph_hybrid']['complete_evidence_rate'] > summary['hybrid']['complete_evidence_rate']
    regression_ok = bool(baseline_results) and (
        statistics.mean(r['recall'] for r in baseline_results if r['mode']=='graph_hybrid') >=
        statistics.mean(r['recall'] for r in baseline_results if r['mode']=='hybrid') - .02)
    summary['release_gate'] = {'relation_improved':improved, 'original_regression_passed':regression_ok,
        'retrieval_gate_passed':improved and regression_ok,
        'default_switch_allowed':False,
        'note':'Retrieval-only gate. Historical business documents have not been graphed, and citation proxies require semantic review; keep the default hybrid. This script never changes defaults.'}
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
