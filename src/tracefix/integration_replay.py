"""Recorded multi-turn memory, Docker interruption and independent verification acceptance."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tracefix.checkpoint import CheckpointStore
from tracefix.dialogue_replay import run_dialogue_replay
from tracefix.memory import atomic_json
from tracefix.memory_replay import RecordedMemoryClient
from tracefix.models import LiteLLMAdapter
from tracefix.onboarding import verify_patch
from tracefix.regression_replay import forbid_live_access
from tracefix.runtime import RunConfig, TraceFixRunner


def worker(output: Path, image_id: str, phase: str) -> dict:
    directory = output / 'docker'
    directory.mkdir(exist_ok=True)
    client = RecordedMemoryClient(recall=True)
    client.sequence[3][1]['patch'] = client.sequence[3][1]['patch'].replace(
        '+    return page + 1', '+    return page if page <= 0 else page + 1')
    runner = TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))
    original = CheckpointStore.save

    def save(store, payload, **kwargs):
        saved = original(store, payload, **kwargs)
        if phase == 'pause' and kwargs['sequence'] == 5:
            raise KeyboardInterrupt()
        return saved

    with forbid_live_access(), patch.object(CheckpointStore, 'save', save):
        if phase == 'pause':
            result = runner.run(RunConfig(
                repo=output / 'learning' / 'source', task='Fix pagination',
                output_dir=directory / 'runs', model_name='offline/integrated', env_file=None,
                execution_backend='docker', docker_profile='ordinary', docker_image_id=image_id,
                docker_recovery_enabled=True, conversation_enabled=True, memory_enabled=True,
                memory_dir=output / 'learning' / 'runs' / 'memory',
                source_import='widget', test_target='tests/test_widget.py',
            ))
            root = Path(result.result_path).parent
            atomic_json(directory / 'location.json', {'run': str(root)})
        else:
            root = Path(json.loads((directory / 'location.json').read_text())['run'])
            inspection = runner.inspect(root)
            if not inspection['resumable']:
                raise AssertionError(inspection)
            client.calls = inspection['step_count']
            result = runner.resume(root)
    expected = 'interrupted' if phase == 'pause' else 'completed'
    if result.status != expected or not client.recalled or result.step_count != client.calls:
        raise AssertionError('durable boundary, actual recall or request accounting was not proven')
    manifest = json.loads((root / 'session.json').read_text(encoding='utf-8'))
    saved = CheckpointStore(root, manifest['identity']).load()
    selection = root / 'memory-selection.json'
    skill = root / 'experience-skills' / 'experience-pagination' / 'SKILL.md'
    record = {
        'pid': os.getpid(), 'phase': phase, 'status': result.status, 'provider_calls': 0,
        'container_id': result.workspace_preparation['container_id'], 'run': str(root),
        'recalled': client.recalled, 'step_count': result.step_count,
        'input_tokens': result.input_tokens, 'output_tokens': result.output_tokens,
        'test_runs': result.test_runs, 'cost_usd': result.cost_usd,
        'memory_status': result.memory_status, 'skills': saved.payload['skills'],
        'selected': json.loads(selection.read_text(encoding='utf-8')),
        'selection_sha256': hashlib.sha256(selection.read_bytes()).hexdigest(),
        'skill_sha256': hashlib.sha256(skill.read_bytes()).hexdigest(),
        'checkpoint_sequence': saved.sequence,
    }
    atomic_json(directory / f'{phase}.json', record)
    return record


def require_removed(container_id: str) -> None:
    result = subprocess.run(['docker', 'inspect', container_id], capture_output=True,
                            text=True, timeout=30, check=False)
    if result.returncode != 1 or not any(
            message in result.stderr.lower()
            for message in ('no such object', 'no such container')):
        raise AssertionError('container deletion was not proven by Docker')


def run_integrated_replay(output: Path, image_id: str) -> dict:
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    learning = run_dialogue_replay(output / 'learning')
    source = output / 'learning' / 'source'
    source_before = hashlib.sha256((source / 'widget.py').read_bytes()).hexdigest()
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                   'PYTHONIOENCODING': 'utf-8'}
    receipts = []
    for phase in ('pause', 'resume'):
        command = [sys.executable, '-m', 'tracefix.integration_replay', '--output', str(output),
                   '--image-id', image_id, '--phase', phase]
        process = subprocess.run(command, cwd=output, env=environment, capture_output=True,
                                 timeout=300, check=False)
        (output / f'{phase}.stdout').write_bytes(process.stdout)
        (output / f'{phase}.stderr').write_bytes(process.stderr)
        atomic_json(output / f'{phase}.command.json', {
            'command': command, 'exit_code': process.returncode})
        if process.returncode:
            raise AssertionError(f'integrated {phase} worker failed')
        receipt = json.loads((output / 'docker' / f'{phase}.json').read_text(encoding='utf-8'))
        require_removed(receipt['container_id'])
        receipts.append(receipt)
    paused, resumed = receipts
    identities = ([item['pid'] for item in learning['processes']]
                  + [item['pid'] for item in receipts])
    if len(set(identities)) != 5 or paused['container_id'] == resumed['container_id']:
        raise AssertionError('separate processes and recreated Docker container were not proven')
    if (not paused['recalled'] or not resumed['recalled'] or not paused['selected']
            or paused['selected'] != resumed['selected'] or paused['skills'] != resumed['skills']
            or paused['selection_sha256'] != resumed['selection_sha256']
            or paused['skill_sha256'] != resumed['skill_sha256']):
        raise AssertionError('loaded experience version changed or was not actually recalled')
    if (paused['status'] != 'interrupted' or paused['step_count'] != 4
            or resumed['status'] != 'completed' or resumed['step_count'] != 8
            or resumed['input_tokens'] != 800 or resumed['output_tokens'] != 80
            or resumed['test_runs'] != 2 or resumed['cost_usd'] != 0
            or resumed['memory_status'].get('status') not in {'verified', 'duplicate'}
            or any(item['provider_calls'] != 0 for item in receipts)):
        raise AssertionError('termination, cumulative resources or extraction was invalid')
    root = Path(resumed['run'])
    with forbid_live_access():
        verification = verify_patch(root)
    atomic_json(output / 'independent.json', verification)
    counts = verification['test']['output']['test_counts']
    verifier = verification['preparation']['container_id']
    if (verification.get('passed') is not True or counts['tests'] != 3 or counts['passed'] != 3
            or any(counts[name] for name in ('failures', 'errors', 'skipped'))
            or verifier in {item['container_id'] for item in receipts}):
        raise AssertionError('independent full-test container verification failed')
    require_removed(verifier)
    if hashlib.sha256((source / 'widget.py').read_bytes()).hexdigest() != source_before:
        raise AssertionError('source baseline was modified')
    summary = {
        'accepted': True, 'scenario': 'memory-dialogue-integration', 'provider_calls': 0,
        'usage_is_simulated': True, 'image_id': image_id, 'learning': learning,
        'docker_processes': receipts, 'independent_container': verifier,
        'independent_passed': True, 'independent_test_counts': counts,
        'implementation_sha256': TraceFixRunner._implementation_sha256(),
        'evidence_sha256': {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.rglob('*.json') if '.git' not in path.parts},
    }
    atomic_json(output / 'integration-summary.json', summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--phase', choices=('pause', 'resume'))
    args = parser.parse_args()
    if args.phase:
        worker(args.output, args.image_id, args.phase)
    else:
        print(json.dumps(run_integrated_replay(args.output, args.image_id), ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
