#!/usr/bin/env python3
"""Explicit sidecar completion using a frozen Martian runtime; offline by default."""
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

sys.dont_write_bytecode = True


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def events_to_entries(path):
    entries = []
    for line in path.read_text().splitlines() if path.exists() else []:
        event = json.loads(line)
        index = event['index']
        if event['event'] == 'reserved':
            require(index == len(entries), 'Duplicate or reordered reservation')
            entries.append(event['entry'])
        else:
            require(event['event'] == 'response' and index == len(entries) - 1,
                    'Unbound or reordered response')
            require(entries[index]['status'] == 'reserved', 'Duplicate response')
            entries[index] = {**entries[index], **event['response']}
    return entries


def success(entry):
    from search_eval.grading import _JudgeBudget
    require(_JudgeBudget.response_cost(entry) is not None,
            'Unknown, non-200, wrong-model or malformed usage is terminal')


class Budget:
    def __init__(self, original, added, main, authorization):
        from search_eval.grading import _JudgeBudget
        self.reserve = _JudgeBudget.RESERVE
        self.cap = Decimal(str(authorization['approved_control_cap_usd']))
        self.combined = Decimal(str(authorization['combined_cap_usd']))
        self.main = main
        self.spent = sum((_JudgeBudget.response_cost(e) or Decimal(0) for e in original + added), Decimal(0))
        for entry in original + added:
            success(entry)

    def admit(self, body):
        from search_eval.grading import _JudgeBudget
        require(body.get('model') == _JudgeBudget.MODEL, 'Unexpected request model')
        require(self.spent + self.reserve <= self.cap, 'Control cap cannot cover $2.60 reservation')
        require(self.main + self.spent + self.reserve <= self.combined,
                'Combined cap cannot cover $2.60 reservation')
        self.spent += self.reserve

    def settle(self, entry):
        from search_eval.grading import _JudgeBudget
        success(entry)
        self.spent += _JudgeBudget.response_cost(entry) - self.reserve


class ReplayTransport:
    """Occurrence-ordered exact replay; new calls have durable reservations first."""
    def __init__(self, entries, path=None, budget=None, sender=None, allowance=None, cap=200):
        self.entries = entries
        self.cursor = 0
        self.path, self.budget, self.sender = path, budget, sender
        self.allowance, self.cap = allowance, cap
        self.added = len(events_to_entries(path)) if path else 0
        self.error = None
        self.lock = asyncio.Lock()
        for entry in entries:
            success(entry)

    async def __call__(self, request):
        async with self.lock:
            return await self.send(request)

    async def send(self, request):
        import httpx
        if self.error:
            raise ValueError(self.error)
        try:
            body = json.loads(request.content)
            identity = {'method': request.method, 'url': str(request.url), 'body': body}
            if self.cursor < len(self.entries):
                entry = self.entries[self.cursor]
                require(all(entry[k] == value for k, value in identity.items()),
                        'Saved full request or occurrence differs')
                self.cursor += 1
                return httpx.Response(200, json=entry['response'], request=request)
            require(self.sender is not None, 'Never-issued request requires explicit live authorization')
            require(self.cursor < self.cap, 'Judge call cap exhausted')
            self.allowance()
            self.budget.admit(body)
            entry = {**identity, 'status': 'reserved', 'reserved_cost_usd': str(self.budget.reserve)}
            append(self.path, {'event': 'reserved', 'index': self.added, 'entry': entry})
            response = await self.sender(request)
            await response.aread()
            payload = {'status': response.status_code}
            try:
                payload['response'] = response.json()
            except ValueError:
                payload.update(response_json_error=True, response_text=response.text)
            append(self.path, {'event': 'response', 'index': self.added, 'response': payload})
            entry.update(payload)
            self.budget.settle(entry)
            self.added += 1
            self.cursor += 1
            return response
        except BaseException as exc:
            self.error = f'Terminal transport: {type(exc).__name__}: {exc}'
            raise


async def pipeline(run, state, attempt, transport, key='offline-not-a-credential'):
    import httpx
    from openai import AsyncOpenAI

    from search_eval.effective_inputs import resolve_judge
    from search_eval.suites import martian
    manifest = read(run / 'manifest.json')
    settings = martian.judge_settings(state['config']['suite_options'], manifest)
    resolved = resolve_judge(settings)
    require(resolved == state['input_identity']['effective_inputs']['judge'], 'Judge effective inputs changed')
    comments = martian.project_github_pages(read(run / attempt['artifact_dir'] / 'github.json'))['comments']
    task = next(t for t in manifest['tasks'] if t['id'] == attempt['task_id'])
    gold, fixed = martian.evaluator_targets(task, martian.gold_records())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = AsyncOpenAI(base_url=resolved['base_url'], api_key=key, max_retries=0,
                             timeout=resolved['transport']['timeout_seconds'], http_client=http)
        extractor, deduper, judge = martian.pipeline_clients(client, settings)
        match = judge.match_comment

        async def checked_match(golden, candidate):
            value = await match(golden, candidate)
            if (value.get('error') or type(value.get('match')) is not bool
                    or type(value.get('confidence')) not in (int, float)):
                transport.error = 'Terminal malformed or failed semantic judge response'
                raise ValueError(transport.error)
            return value

        judge.match_comment = checked_match

        def matrix(count):
            require(transport.cursor + count <= transport.cap, 'Complete matrix exceeds 200-call cap')

        saved = await martian.run_pipeline(comments, gold, extractor, deduper, judge, settings,
                                           fixed_target=fixed, before_judging=matrix)
    require(not transport.error and transport.cursor >= len(transport.entries), 'Unconsumed or failed transport')
    await martian.replay_pipeline(saved, comments, gold, settings, fixed_target=fixed)
    return saved


def original_entries(run, state):
    return [e for a in state['attempts'] for e in read(run / a['artifact_dir'] / 'judge-http.json', [])]


def check_original(source, run, authorization, main_run):
    from search_eval.core import frozen_manifest, verify_run_code
    from search_eval.grading import _JudgeBudget
    state = read(run / 'state.json')
    verify_run_code(state, source)
    frozen_manifest(run, state)
    require(state['fingerprint'] == authorization['run_fingerprint'], 'Authorization run identity mismatch')
    require(state['input_identity']['code'] == authorization['original_code_sha256'], 'Source identity changed')
    actual = {str(p.relative_to(run)): sha(p) for p in run.rglob('*') if p.is_file()}
    require(actual == authorization['original_files'], 'Original evidence inventory changed')
    require(state['config']['suite_options']['max_judge_usd'] == authorization['original_config_cap_usd'], 'Original cap changed')
    require(state['config']['suite_options']['max_judge_calls_per_attempt'] == 200, 'Frozen call cap changed')
    main_state = read(main_run / 'state.json')
    verify_run_code(main_state, source)
    frozen_manifest(main_run, main_state)
    entries = original_entries(main_run, main_state)
    for entry in entries:
        success(entry)
    main = sum((_JudgeBudget.response_cost(e) for e in entries), Decimal(0))
    require(main == Decimal(str(authorization['main_recorded_cost_usd'])), 'Main spend changed')
    require(main <= Decimal(str(authorization['main_reserve_cap_usd'])), 'Main cap exceeded')
    controls = original_entries(run, state)
    for entry in controls:
        success(entry)
    require(sum((_JudgeBudget.response_cost(e) for e in controls), Decimal(0)) ==
            Decimal(str(authorization['controls_recorded_cost_usd'])), 'Original control spend changed')
    for attempt in state['attempts']:
        folder = run / attempt['artifact_dir']
        failure = read(folder / 'martian-grading-failure.json')
        if failure:
            require(failure['status'] == 'failed' and failure['retry_allowed'] is False
                    and failure['attempt_id'] == attempt['id']
                    and failure['ledger_sha256'] == sha(folder / 'judge-http.json')
                    and failure['cache_sha256'] == sha(folder / 'martian-grading.json'),
                    'Original terminal evidence changed')
    return state, main, controls


def verify_authorization(auth):
    require(auth.get('authorization') and auth.get('allowed_action'), 'Missing explicit authorization')
    control, main, combined, credit = (Decimal(str(auth[k])) for k in
        ('approved_control_cap_usd', 'main_reserve_cap_usd', 'combined_cap_usd', 'prelaunch_verified_credit_usd'))
    require(control > 0 and main > 0 and control + main == combined and combined <= credit,
            'Invalid funding amendment or insufficient prepaid credit')
    require(auth['purchases_authorized'] is False and auth['new_review_attempts_authorized'] is False,
            'Only existing-credit grading completion is supported')


@contextmanager
def frozen_runtime(source):
    sys.path.insert(0, str(source / 'src'))
    from search_eval.suites import martian
    archive = source / 'assets/benchmarks.tar.gz'
    if not archive.exists():
        require(martian.UPSTREAM.is_dir(), 'Frozen benchmark runtime missing')
        yield
        return
    expected = next(x.split()[0] for x in (source / 'assets/SHA256SUMS').read_text().splitlines()
                    if x.endswith('benchmarks.tar.gz'))
    require(sha(archive) == expected, 'Benchmark archive changed')
    with tempfile.TemporaryDirectory(prefix='martian-frozen-') as temp:
        with tarfile.open(archive) as tar:
            tar.extractall(temp, members=[m for m in tar.getmembers() if m.name.startswith('vendor/martian/')], filter='data')
        martian.UPSTREAM = Path(temp) / 'vendor/martian/offline'
        yield


def deny_network(*args, **kwargs):
    raise RuntimeError('Offline mode prohibits network')


async def complete(args, source, run, auth, state, main, originals):
    import httpx

    from search_eval.core import Experiment, verify_allowance
    config = Experiment.model_validate(state['config'])
    sidecar = args.sidecar.resolve()
    require(not sidecar.is_relative_to(source), 'Sidecar must be outside original source')
    binding = {'version': 1, 'authorization_sha256': sha(args.authorization) if args.authorization else read(sidecar / 'binding.json')['authorization_sha256'],
               'helper_sha256': sha(Path(__file__)), 'original_files': auth['original_files'],
               'run_fingerprint': state['fingerprint'], 'main_run_id': args.main_run.name,
               'public_scope': {k: auth[k] for k in (
                   'run_id', 'run_fingerprint', 'original_code_sha256', 'original_files',
                   'original_config_cap_usd', 'approved_control_cap_usd', 'main_reserve_cap_usd',
                   'combined_cap_usd', 'prelaunch_verified_credit_usd',
                   'main_recorded_cost_usd', 'controls_recorded_cost_usd')}}
    if (sidecar / 'binding.json').exists():
        require(read(sidecar / 'binding.json') == binding, 'Sidecar binding changed')
    elif args.mode == 'complete':
        write_new(sidecar / 'binding.json', binding)
    else:
        raise ValueError('Missing sidecar binding')
    require(not list(sidecar.glob('*/failure.json')), 'Prior reconciliation failure is terminal')
    all_added = [e for a in state['attempts'] for e in events_to_entries(sidecar / a['id'] / 'http.jsonl')]
    budget = Budget(originals, all_added, main, auth)

    def allowance():
        verify_allowance(config, source, len(state['attempts']))
        check_original(source, run, auth, args.main_run.resolve())
        require(sha(args.authorization) == binding['authorization_sha256'], 'Authorization changed')

    key = os.environ.get('MARTIAN_API_KEY') if args.mode == 'complete' else None
    if args.mode == 'complete':
        require(key, 'Set MARTIAN_API_KEY in the process environment')
        allowance()
    receipts = []
    async with httpx.AsyncClient(trust_env=False, timeout=120) as live:
        for attempt in state['attempts']:
            require(attempt['status'] == 'completed', 'Review inputs are not complete')
            folder, out = run / attempt['artifact_dir'], sidecar / attempt['id']
            original = read(folder / 'judge-http.json', [])
            added = events_to_entries(out / 'http.jsonl')
            target = folder / 'martian-grading.json'
            cached = read(target) if target.exists() else read(out / 'martian-grading.json')
            can_issue = args.mode == 'complete' and cached is None
            transport = ReplayTransport(original + added, out / 'http.jsonl', budget,
                                        live.send if can_issue else None, allowance)
            try:
                saved = await pipeline(run, state, attempt, transport, key or 'offline-not-a-credential')
                if cached is not None:
                    require(saved == cached, 'Raw HTTP replay differs from completed cache')
                elif can_issue:
                    write_new(out / 'martian-grading.json', saved)
                else:
                    raise ValueError('Missing completed cache')
                receipt = {'attempt_id': attempt['id'], 'original_ledger_sha256': sha(folder / 'judge-http.json'),
                           'original_failure_sha256': sha(folder / 'martian-grading-failure.json'),
                           'sidecar_ledger_sha256': sha(out / 'http.jsonl'),
                           'cache_sha256': sha(target if target.exists() else out / 'martian-grading.json'),
                           'original_requests': len(original), 'new_requests': len(events_to_entries(out / 'http.jsonl')),
                           'validated_request_occurrences': transport.cursor, 'raw_replay_and_native_cache_validated': True}
                if (out / 'receipt.json').exists():
                    require(read(out / 'receipt.json') == receipt, 'Completion receipt changed')
                elif args.mode == 'complete':
                    write_new(out / 'receipt.json', receipt)
                else:
                    raise ValueError('Missing completion receipt')
                receipts.append(receipt)
            except BaseException as exc:
                if can_issue and not (out / 'failure.json').exists():
                    write_new(out / 'failure.json', {'terminal': True, 'reason': f'{type(exc).__name__}: {exc}',
                                                     'ledger_sha256': sha(out / 'http.jsonl')})
                raise
    check_original(source, run, auth, args.main_run.resolve())
    return {'complete': True, 'run_id': auth['run_id'], 'authorization_sha256': binding['authorization_sha256'],
            'original_evidence_unchanged': True, 'controls_actual_usd': str(budget.spent),
            'main_actual_usd': str(main), 'combined_actual_usd': str(main + budget.spent), 'attempts': receipts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['complete', 'validate', 'report'])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--authorization', type=Path)
    parser.add_argument('--sidecar', type=Path, required=True)
    parser.add_argument('--main-run', type=Path, required=True)
    parser.add_argument('--derived-report', type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    if args.mode == 'complete':
        require(args.authorization is not None, 'Live completion requires --authorization')
        auth = read(args.authorization)
        verify_authorization(auth)
    else:
        auth = read(args.sidecar / 'binding.json')['public_scope']
    run = source / 'runs' / auth['run_id']
    if args.mode != 'complete':
        socket.create_connection = socket.socket.connect = socket.socket.connect_ex = deny_network
    with frozen_runtime(source):
        from search_eval.core import lock
        with lock(source / '.gateway/runner.lock'):
            state, main_cost, originals = check_original(source, run, auth, args.main_run.resolve())
            result = asyncio.run(complete(args, source, run, auth, state, main_cost, originals))
            if args.mode == 'report':
                require(args.derived_report is not None, '--derived-report required')
                destination = args.derived_report.resolve()
                require(not destination.exists() and not destination.is_relative_to(source), 'Derived destination must be new and external')
                shutil.copytree(run, destination)
                for attempt in state['attempts']:
                    out, target = args.sidecar / attempt['id'], destination / attempt['artifact_dir']
                    if (out / 'martian-grading.json').exists():
                        shutil.copy2(out / 'martian-grading.json', target / 'martian-grading.json')
                        (target / 'martian-grading-failure.json').unlink(missing_ok=True)
                        original = read(target / 'judge-http.json', [])
                        (target / 'judge-http.json').write_text(json.dumps(original + events_to_entries(out / 'http.jsonl'), indent=2) + '\n')
                write_new(destination / 'reconciliation-provenance.json', {**result, 'derived_view': True,
                           'original_run_id': auth['run_id'], 'sidecar_binding_sha256': sha(args.sidecar / 'binding.json'),
                           'note': 'Original failures retained in original run. This validated derived view uses explicit reconciliation caches.'})
                from search_eval.report import build_report
                result['derived_report'] = build_report(destination)
                check_original(source, run, auth, args.main_run.resolve())
            print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
