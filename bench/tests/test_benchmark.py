import asyncio
import importlib.util
import json
from pathlib import Path
import random
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orbench
import controlled_replay as replay


def event(delta=None, finish=None, usage=None):
    return 'data: ' + json.dumps({'choices': [{'delta': delta or {}, 'finish_reason': finish}],
                                  'usage': usage}) + '\n\n'


class Stream(httpx.AsyncByteStream):
    def __init__(self, parts): self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, float): await asyncio.sleep(part)
            elif isinstance(part, Exception): raise part
            else: yield part.encode()


@pytest.fixture
def bench(tmp_path):
    b = orbench.Bench(tmp_path / 'requests.csv')
    yield b
    b.f.close()


def call(bench, parts, status=200):
    async def invoke():
        async def handle(request): return httpx.Response(status, stream=Stream(parts))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await orbench.one_request(client, bench, 1, random.Random(1),
                                             orbench.Sessions(random.Random(2)))
    return asyncio.run(invoke())


def test_role_event_is_not_first_token(bench):
    assert call(bench, [event({'role': 'assistant'}), .03, event({'content': 'OK'}),
                        event(finish='stop', usage={'completion_tokens': 1, 'prompt_tokens': 50}),
                        'data: [DONE]\n\n'])
    assert bench.rows[0]['ttft_s'] >= .025
    assert bench.rows[0]['completed']
    assert bench.inflight == 0


@pytest.mark.parametrize('tail', [[], ['data: [DONE]\n\n'],
                                  [event(finish='stop')],
                                  ['data: {"error":{"message":"failure"}}\n\n'],
                                  ['data: {broken\n\n'], [httpx.ReadError('disconnected')]])
def test_partial_or_invalid_stream_is_failure(bench, tail):
    assert not call(bench, [event({'content': 'partial'}), *tail])
    row = bench.rows[0]
    assert row['status'] == 200 and row['ttft_s'] is not None
    assert not row['completed'] and row['err'] and row['tok_per_s'] is None
    s = orbench.summarise(bench, 1, row['ts'] - .1)
    assert s['completed'] == 0 and s['othererr'] == 1


def test_complete_empty_reply_and_missing_usage_do_not_crash_summary(bench):
    assert call(bench, [event(finish='stop'), 'data: [DONE]\n\n'])
    s = orbench.summarise(bench, 1, bench.rows[0]['ts'] - .1)
    assert s['completed'] == 1 and s['med_tok_s'] == 0 and s['ttft_p50'] == 0


def test_http_refusal_is_not_retried(bench):
    assert not call(bench, ['refused'], status=429)
    s = orbench.summarise(bench, 1, bench.rows[0]['ts'] - .1)
    assert s['offered'] == 1 and s['r429'] == 1 and s['othererr'] == 0


def test_sessions_are_exclusive_and_keep_full_replies():
    rng = random.Random(3)
    sessions = orbench.Sessions(rng)
    first, _ = sessions.take(12)
    second, _ = sessions.take(12)
    assert first != second  # first is still running
    reply = {'role': 'assistant', 'content': 'x' * 4000, 'reasoning': 'reasoning'}
    sessions.record_reply(first, reply)
    rng.random = lambda: 0
    reused, msgs = sessions.take(12)
    assert reused == first and msgs[-2] == reply
    assert sessions.pool[first]['busy']
    sessions.discard(first)
    assert first not in sessions.pool


def test_drain_cancels_joins_and_records_every_started_request(bench):
    async def invoke():
        async def handle(request):
            return httpx.Response(200, stream=Stream([event({'content': 'partial'}), 10.0]))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(RuntimeError, match='drain_timeout'):
                await orbench.run_rate(client, bench, 1000, .02, random.Random(1),
                                       orbench.Sessions(random.Random(2)), .01)
        assert bench.offered[1000] > 0
        assert len(bench.rows) == bench.offered[1000]
        assert all(r['err'] == 'cancelled_unfinished' and not r['completed'] for r in bench.rows)
        assert bench.inflight == 0
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(invoke())


def test_fixed_fixture_reproducible_and_salt_is_early():
    a = replay.fixture(42, 'arm00001', 'long', 3, 2, 1, 'model')
    b = replay.fixture(42, 'arm00001', 'long', 3, 2, 1, 'model')
    c = replay.fixture(42, 'arm00002', 'long', 3, 2, 1, 'model')
    assert a == b
    replay.validate_fixture(a)
    assert [r['at_s'] for r in a['requests']] == [r['at_s'] for r in c['requests']]
    assert 'arm00001' in a['requests'][0]['body']['messages'][0]['content'][:30]
    assert 'arm00002' in c['requests'][0]['body']['messages'][0]['content'][:30]
    assert len(a['requests'][0]['body']['messages']) == 2
    assert len(next(r for r in a['requests'] if r['session'] == 0 and r['turn'] == 2)['body']['messages']) == 4


def test_replay_preserves_causality_with_independent_sessions(monkeypatch, bench):
    active = set(); order = []; peak = 0
    async def request(client, bench, rate, rng, sessions, request):
        nonlocal peak
        sid = request['session']
        assert sid not in active
        active.add(sid); peak = max(peak, len(active))
        order.append((sid, request['turn']))
        await asyncio.sleep(.005)
        active.remove(sid)
        return True
    monkeypatch.setattr(orbench, 'one_request', request)
    data = replay.fixture(3, 'test', 'long', 2, 2, 1, 'model')
    for req in data['requests']: req['at_s'] = 0
    receipt = asyncio.run(replay.replay(None, data, bench, 1))
    assert peak == 2 and not active
    assert not receipt['drain_timed_out']
    for sid in range(2): assert [turn for s, turn in order if s == sid] == [1, 2]


def test_worker_topology_gate():
    async def invoke():
        async def handle(request):
            return httpx.Response(200, json={'workers': [
                {'worker_id': str(i), 'url': f'http://worker{i}:30000',
                 'status': 'active', 'disagg_mode': 'PREFILL' if i < 3 else 'DECODE'}
                for i in range(6)]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(RuntimeError, match='exactly 4 prefill'):
                await replay.get_workers(client, 'http://infera:8000')
    asyncio.run(invoke())


def test_counters_keep_role_identity_and_detect_resets():
    metrics = replay.parse_metrics('''# HELP vllm:request_success_total Request count
vllm:request_success_total{finish_reason="stop"} 5
vllm:request_success_total{finish_reason="length"} 2
vllm:prefix_cache_queries_total 100
vllm:prefix_cache_hits_total 80
process_start_time_seconds 123
''')
    assert metrics['vllm:request_success_total'] == 7
    zero = dict.fromkeys(metrics, 0); zero['process_start_time_seconds'] = 123
    worker = {'worker_id': 'w0', 'role': 'prefill', 'url': 'http://w0:30000'}
    result = replay.counter_deltas({'w0': zero}, {'w0': metrics}, [worker])[0]
    assert result['role'] == 'prefill' and result['prefix_hit_ratio'] == .8
    metrics['process_start_time_seconds'] = 124
    with pytest.raises(RuntimeError, match='restarted'):
        replay.counter_deltas({'w0': zero}, {'w0': metrics}, [worker])


def test_invalid_usage_does_not_lose_request_accounting(bench):
    assert not call(bench, [event({'content': 'OK'}, finish='stop', usage=['bad']), 'data: [DONE]\n\n'])
    assert len(bench.rows) == 1 and bench.rows[0]['err'] == 'invalid_stream'


def test_offered_rate_excludes_drain_time(bench):
    assert call(bench, [event({'content': 'OK'}, finish='stop'), 'data: [DONE]\n\n'])
    bench.durations[1] = 10
    summary = orbench.summarise(bench, 1, bench.rows[0]['ts'] - 30)
    assert summary['offered_rps'] == .1


@pytest.mark.parametrize('compressed', [False, True])
def test_full_replay_writes_worker_and_request_receipts(tmp_path, monkeypatch, compressed):
    import gzip
    from types import SimpleNamespace
    data = replay.fixture(3, 'test', 'short', 2, 1, 10000, 'model')
    fixture_path = tmp_path / ('input.json.gz' if compressed else 'input.json')
    payload = json.dumps(data).encode()
    fixture_path.write_bytes(gzip.compress(payload, mtime=0) if compressed else payload)
    manifest = tmp_path / 'deployment.yaml'; manifest.write_text('kind: List\nitems: []\n')
    out = tmp_path / 'receipts'
    workers = [{'worker_id': str(i), 'url': f'http://worker{i}:30000', 'status': 'active',
                'disagg_mode': 'PREFILL' if i < 4 else 'DECODE'} for i in range(6)]
    sent = 0

    async def handle(request):
        nonlocal sent
        if request.url.path == '/v1/workers':
            return httpx.Response(200, json={'workers': workers})
        if request.url.path == '/metrics':
            return httpx.Response(200, text='\n'.join([
                'vllm:num_requests_running 0', 'vllm:num_requests_waiting 0',
                f'vllm:request_success_total {sent}', f'vllm:prefix_cache_queries_total {sent * 100}',
                f'vllm:prefix_cache_hits_total {sent * 50}', 'process_start_time_seconds 123']))
        assert request.url.path == '/v1/chat/completions'
        sent += 1
        return httpx.Response(200, stream=Stream([
            event({'content': 'OK'}, finish='stop', usage={'prompt_tokens': 100, 'completion_tokens': 1}),
            'data: [DONE]\n\n']))

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs:
                        client_class(transport=httpx.MockTransport(handle), **kwargs))
    args = SimpleNamespace(fixture=fixture_path, manifest=manifest, out=out, arm='test',
                           cache_state='fresh-prefix', base='http://infera:8000',
                           idle_timeout=5, drain=1)
    assert asyncio.run(replay.run(args)) == 0
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['valid'] and summary['planned'] == summary['dispatched'] == summary['completed'] == 2
    assert len(summary['workers']) == 6
    assert len(list(out.glob('*.metrics'))) == 12
    assert summary['workers'][0]['prefix_hit_ratio'] == .5
    assert summary['prompt_tokens'] == 200 and summary['completion_tokens'] == 2
    assert summary['input_tokens_per_s'] == pytest.approx(100 * summary['output_tokens_per_s'])
    assert summary['usage_complete']
    receipt_name = 'fixture.json.gz' if compressed else 'fixture.json'
    assert (out / receipt_name).read_bytes() == fixture_path.read_bytes()


def test_default_long_fixture_fits_configmap():
    import base64
    import gzip
    data = replay.fixture(20260910, 'prep0001', 'long', 32, 4, .5, 'moonshotai/Kimi-K3')
    payload = gzip.compress(json.dumps(data).encode(), mtime=0)
    # Reserve room for both programs, deployment provenance and YAML metadata.
    assert len(base64.b64encode(payload)) < 700000


def test_input_heavy_fixture_shuffles_turns_and_preserves_prefixes():
    a = replay.fixture(42, 'heavy001', 'input-heavy', 12, 3, 4, 'model')
    b = replay.fixture(42, 'heavy001', 'input-heavy', 12, 3, 4, 'model')
    assert a == b
    replay.validate_fixture(a)
    by_time = sorted(a['requests'], key=lambda r: r['at_s'])
    orders = [[r['session'] for r in by_time if r['turn'] == t] for t in (1, 2, 3)]
    assert orders[0] != orders[1] != orders[2]
    for sid in range(12):
        turns = [r for r in a['requests'] if r['session'] == sid]
        assert turns[1]['body']['messages'][:2] == turns[0]['body']['messages']
        assert turns[2]['body']['messages'][:4] == turns[1]['body']['messages']
        assert [r['turn'] for r in turns] == [1, 2, 3]
        assert all('heavy001' in r['body']['messages'][0]['content'][:30] for r in turns)
        assert all(128 <= r['body']['max_tokens'] <= 512 for r in turns)
    c = replay.fixture(42, 'heavy002', 'input-heavy', 12, 3, 4, 'model')
    assert [r['at_s'] for r in a['requests']] == [r['at_s'] for r in c['requests']]
