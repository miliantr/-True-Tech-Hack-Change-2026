"""Measure retrieved evidence and capture optional live answers for human review."""
import argparse
import csv
import json
import math
import time
from dataclasses import replace
from pathlib import Path
from statistics import median

from app import load_dotenv
from config import Config, env
from consultant import Consultant, parse_dialogue
from retrieval import Chunk, locate_kb

ROOT=Path(__file__).resolve().parent
EXPECTED={
    '1':['запад-восток'], '2':['вертикальные'], '3':['наращива','поэтап'],
    '4':['шифер'], '5':['сим-картами'], '6':['цех'], '7':['ретейл'],
    '8':['тариф'], '9':['год производства'], '10':['сравнить два периода','мощност'],
    '11':['реактив'], '12':['750','аудит'], '13':['невыгод','нецелесообраз'],
    '14':['накопител','дневн'], '15':['фотограф','арендатор'],
    '16':['дневное','года'], '17':['двухставоч','мощност'], '18':['прибор'],
}


def load_cases(suite, data_dir):
    if suite == 'demo':
        with (data_dir/'demo_questions.csv').open(encoding='utf-8-sig', newline='') as file:
            return [{**row, 'category': 'demo', 'evidence': [['', term] for term in EXPECTED[row['id']]]}
                    for row in csv.DictReader(file)]
    cases = json.loads((ROOT/'tests/scenarios.json').read_text(encoding='utf-8'))
    return [case for case in cases if case['split'] == suite and case['category'] != 'lead']


def normalized(text):
    return ' '.join(text.casefold().replace('ё', 'е').split())


def evidence_present(chunks, source, terms):
    return any(chunk.source.endswith(tuple(source.split('|'))) and
               any(normalized(term) in normalized(chunk.heading + ' ' + chunk.text) for term in terms.split('|'))
               for chunk in chunks)


def evaluate(rows, kb, config, generate=False):
    start = time.perf_counter()
    engine = Consultant(kb, config, deadline=time.monotonic()+180)
    index_ms = round((time.perf_counter()-start)*1000, 2)
    results = []
    for row in rows:
        messages = parse_dialogue(row['question'])
        start = time.perf_counter()
        if generate:
            reply = engine.answer(messages)
            chunks = [Chunk(**source) for source in reply['debug']['sources']]
            warnings = reply['debug']['warnings']
            retrieval_ms = reply['debug']['retrieval_ms']
        else:
            chunks, warnings = engine.retrieve(messages, time.monotonic()+60)
            retrieval_ms = round((time.perf_counter()-start)*1000, 2)
        missing = [evidence for evidence in row['evidence'] if not evidence_present(chunks, *evidence)]
        result = {'id': row['id'], 'category': row['category'], 'scored': bool(row['evidence']),
                  'missing': missing, 'warnings': warnings, 'retrieval_ms': retrieval_ms,
                  'context_chars': sum(len(chunk.heading) + len(chunk.text) for chunk in chunks),
                  'sources': [chunk.source + ' / ' + chunk.heading for chunk in chunks]}
        if generate:
            result.update(answer=reply['answer'], mode=reply['mode'], elapsed_ms=reply['debug']['elapsed_ms'],
                          question=row['question'], expect=row.get('expect'), avoid=row.get('avoid'))
        results.append(result)
    times = sorted(r['retrieval_ms'] for r in results)
    scored = [r for r in results if r['scored']]
    return {'cases': len(results), 'scored': len(scored), 'complete': sum(not r['missing'] for r in scored),
            'degraded_cases': sum(bool(r['warnings']) for r in results), 'index_ms': index_ms,
            'p50_ms': median(times), 'p95_ms': times[math.ceil(len(times)*.95)-1],
            'median_context_chars': median(r['context_chars'] for r in results), 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('demo','development','holdout'), default='demo')
    parser.add_argument('--compare', action='store_true')
    parser.add_argument('--proxy', action='store_true', help='Use the authorized proxy for embedding/reranking comparisons')
    parser.add_argument('--generate', action='store_true', help='Save live answers for manual scoring')
    args = parser.parse_args()
    if args.generate and (not args.proxy or args.compare):
        parser.error('--generate requires --proxy and a single configuration')
    load_dotenv(ROOT/'.env')
    config = Config.from_env() if args.proxy else Config()
    if args.proxy and not config.live:
        parser.error('Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env or the environment')
    data_dir = Path(env('DATA_DIR', default=str(ROOT/'Данные')))
    rows = load_cases(args.suite, data_dir)
    kb = locate_kb(data_dir)
    variants = {'configured' if args.proxy else 'lexical-6': config}
    if args.compare:
        lexical = replace(config, retrieval='lexical', rerank=False, top_n=6)
        variants = {'lexical-6': lexical}
        if args.proxy:
            variants['hybrid'] = replace(lexical, retrieval='hybrid')
            variants['hybrid-rerank'] = replace(lexical, retrieval='hybrid', rerank=True)
        else:
            variants['lexical-3'] = replace(lexical, top_n=3)
            variants['lexical-10'] = replace(lexical, top_n=10)
    output = {'suite': args.suite, 'description': 'Evidence coverage and timing; manual answer quality is separate.', 'variants': {}}
    for name, variant in variants.items():
        result = evaluate(rows, kb, variant, args.generate)
        output['variants'][name] = result
        print(f"{name}: evidence {result['complete']}/{result['scored']}, p50 {result['p50_ms']:.2f} ms, "
              f"p95 {result['p95_ms']:.2f} ms, median context {result['median_context_chars']:g} chars, "
              f"degraded {result['degraded_cases']}")
    kind = 'live-answers' if args.generate else 'proxy' if args.proxy else 'retrieval'
    destination = ROOT/f'test-results/{kind}-{args.suite}.json'
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(destination)


if __name__ == '__main__':
    main()
