"""Receipt fault injection supplements the installed, real Linux Docker scenario."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix import integration_replay as replay


@pytest.mark.parametrize('fault', [
    None, 'learning', 'learning-provider', 'worker', 'daemon', 'alive', 'pid',
    'container', 'recall', 'selection', 'skills', 'selection-hash', 'skill-hash',
    'activation', 'loaded', 'version', 'bytes', 'paused', 'steps', 'input', 'output',
    'tests', 'cost', 'memory', 'provider', 'verification', 'count', 'passed',
    'failure', 'error', 'skipped', 'verifier-container', 'source',
])
def test_controller_accepts_only_complete_independent_evidence(tmp_path, monkeypatch, fault):
    output = tmp_path / 'acceptance'

    def learn(directory):
        source = directory / 'source'
        source.mkdir(parents=True)
        (source / 'widget.py').write_text('baseline', encoding='utf-8')
        return {'accepted': fault != 'learning',
                'provider_calls': int(fault == 'learning-provider'),
                'processes': [{'pid': number} for number in (1, 2, 3)]}

    def run(command, **kwargs):
        if command[:2] == ['docker', 'inspect']:
            return SimpleNamespace(returncode=0 if fault == 'alive' else 1,
                                   stderr='daemon unavailable' if fault == 'daemon'
                                   else 'Error: No such object: missing')
        phase = command[-1]
        directory = output / 'docker'
        directory.mkdir(exist_ok=True)
        record = {
            'pid': 4 if phase == 'pause' else (3 if fault == 'pid' else 5),
            'container_id': 'old' if phase == 'pause' or fault == 'container' else 'new',
            'recalled': True, 'selected': [{'key': 'pagination', 'version': 1}],
            'skills': {'activated': ['experience-pagination'], 'loaded_bytes': 100,
                       'loaded': [{'name': 'experience-pagination', 'version': '1'}]},
            'selection_sha256': 'selection', 'skill_sha256': 'skill',
            'status': 'interrupted' if phase == 'pause' else 'completed',
            'step_count': 4 if phase == 'pause' else 8,
            'input_tokens': 800, 'output_tokens': 80, 'test_runs': 2, 'cost_usd': 0,
            'memory_status': {'status': 'duplicate'}, 'provider_calls': 0,
            'run': str(directory),
        }
        if fault in {'activation', 'loaded', 'version', 'bytes'}:
            if fault == 'activation':
                record['skills']['activated'] = []
            elif fault == 'loaded':
                record['skills']['loaded'] = []
            elif fault == 'version':
                record['skills']['loaded'][0]['version'] = '2'
            else:
                record['skills']['loaded_bytes'] = 0
        if phase == 'resume':
            changes = {
                'recall': ('recalled', False), 'selection': ('selected', []),
                'skills': ('skills', {}), 'selection-hash': ('selection_sha256', 'other'),
                'skill-hash': ('skill_sha256', 'other'), 'steps': ('step_count', 9),
                'input': ('input_tokens', 900), 'output': ('output_tokens', 90),
                'tests': ('test_runs', 1), 'cost': ('cost_usd', 1),
                'memory': ('memory_status', {'status': 'candidate'}),
                'provider': ('provider_calls', 1),
            }
            if fault in changes:
                key, value = changes[fault]
                record[key] = value
        elif fault == 'paused':
            record['status'] = 'completed'
        (directory / f'{phase}.json').write_text(json.dumps(record), encoding='utf-8')
        return SimpleNamespace(returncode=int(fault == 'worker'), stdout=b'raw output',
                               stderr=b'raw error')

    def verify(root):
        assert root == output / 'docker'
        counts = {'tests': 3, 'passed': 3, 'failures': 0, 'errors': 0, 'skipped': 0}
        for violation, key in [('count', 'tests'), ('passed', 'passed'),
                               ('failure', 'failures'), ('error', 'errors'),
                               ('skipped', 'skipped')]:
            if fault == violation:
                counts[key] = 1
        if fault == 'source':
            (output / 'learning' / 'source' / 'widget.py').write_text('mutated')
        return {'passed': fault != 'verification',
                'test': {'output': {'test_counts': counts}},
                'preparation': {'container_id': 'new' if fault == 'verifier-container'
                                else 'independent'}}

    monkeypatch.setattr(replay, 'run_dialogue_replay', learn)
    monkeypatch.setattr(replay.subprocess, 'run', run)
    monkeypatch.setattr(replay, 'verify_patch', verify)
    if fault:
        with pytest.raises(AssertionError):
            replay.run_integrated_replay(output, 'image')
        assert not (output / 'integration-summary.json').exists()
        if fault == 'worker':
            assert json.loads((output / 'pause.command.json').read_text())['exit_code'] == 1
    else:
        summary = replay.run_integrated_replay(output, 'image')
        assert summary['accepted'] and summary['provider_calls'] == 0
        assert 'pause.stdout' in summary['evidence_sha256']
        assert summary['environment']['executable']
        assert summary['environment']['platform'] == replay.sys.platform
        assert summary['source_widget_sha256']
        with pytest.raises(FileExistsError):
            replay.run_integrated_replay(output, 'image')


@pytest.mark.parametrize('phase,violation', [
    ('pause', None), ('resume', None), ('resume', 'unsafe'),
    ('pause', 'terminal'), ('resume', 'recall'), ('resume', 'accounting'),
])
def test_worker_requires_durable_boundary_actual_recall_and_accounting(
    tmp_path, monkeypatch, phase, violation,
):
    directory = tmp_path / 'docker'
    root = directory / 'run'
    root.mkdir(parents=True)
    identity = {'fixture': True}
    (root / 'session.json').write_text(json.dumps({'identity': identity}))
    (directory / 'location.json').write_text(json.dumps({'run': str(root)}))
    (root / 'memory-selection.json').write_text('[{"version":1}]')
    skill = root / 'experience-skills' / 'experience-pagination' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('experience')
    client = SimpleNamespace(sequence=[[None, {}] for _ in range(4)], calls=0, recalled=True)
    client.sequence[3][1]['patch'] = '+    return page + 1'
    monkeypatch.setattr(replay, 'RecordedMemoryClient', lambda **kwargs: client)

    class Runner:
        def __init__(self, factory):
            assert callable(factory)

        def inspect(self, requested):
            assert requested == root
            return {'resumable': violation != 'unsafe', 'step_count': 4}

        def finish(self):
            status = 'completed'
            store = replay.CheckpointStore(root, identity)
            try:
                store.save({'skills': {'loaded': ['experience']}}, sequence=5)
            except KeyboardInterrupt:
                status = 'interrupted'
            client.calls = 4 if phase == 'pause' else 8
            client.recalled = violation != 'recall'
            return SimpleNamespace(
                result_path=str(root / 'result.json'),
                status='failed' if violation == 'terminal' else status,
                step_count=9 if violation == 'accounting' else client.calls,
                input_tokens=800, output_tokens=80, test_runs=2, cost_usd=0,
                memory_status={'status': 'duplicate'},
                workspace_preparation={'container_id': 'owned'},
            )

        def run(self, config):
            assert config.memory_enabled and config.conversation_enabled
            assert config.docker_recovery_enabled and config.docker_profile == 'ordinary'
            return self.finish()

        def resume(self, requested):
            assert requested == root and client.calls == 4
            return self.finish()

    monkeypatch.setattr(replay, 'TraceFixRunner', Runner)
    if violation:
        with pytest.raises(AssertionError):
            replay.worker(tmp_path, 'image', phase)
        assert not (directory / f'{phase}.json').exists()
    else:
        receipt = replay.worker(tmp_path, 'image', phase)
        assert receipt['checkpoint_sequence'] == 5
        assert receipt['provider_calls'] == 0
        assert client.sequence[3][1]['patch'] == '+    return page if page <= 0 else page + 1'


@pytest.mark.parametrize('phase', [None, 'pause', 'resume'])
def test_command_dispatch(tmp_path, monkeypatch, capsys, phase):
    calls = []
    monkeypatch.setattr(replay, 'worker', lambda *args: calls.append(args))
    monkeypatch.setattr(replay, 'run_integrated_replay',
                        lambda *args: calls.append(args) or {'accepted': True})
    argv = ['integration', '--output', str(tmp_path), '--image-id', 'image']
    if phase:
        argv += ['--phase', phase]
    monkeypatch.setattr(replay.sys, 'argv', argv)
    assert replay.main() == 0
    assert calls == [(Path(tmp_path), 'image', phase) if phase else (Path(tmp_path), 'image')]
    assert bool(capsys.readouterr().out) == (phase is None)
