#!/usr/bin/env python3.12
"""Run the real review/extraction browser path once, with a five-minute limit."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import requests
from langgraph.checkpoint.sqlite import SqliteSaver
from playwright.sync_api import sync_playwright
from api.runtime import graph_config
from epi_agent.artifacts import StateArtifactStore
from utils.env_loader import load_app_environment


def main(*, scenario='deselection'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--environment-root', type=Path, required=True)
    parser.add_argument('--model', help='Exact real model ID to select through the browser before sending the query.')
    parser.add_argument('--study-root', type=Path, help='Prepared real study installation root.')
    parser.add_argument('--embedding-profile', help='Real embedding profile matching the installed packages.')
    args = parser.parse_args()
    artifacts = args.artifact_dir.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    marker = artifacts / 'started'
    with marker.open('x') as handle:
        handle.write('Run once. Do not automatically rerun this smoke.\n')
    started = time.monotonic()
    deadline = started + 295
    load_app_environment(args.environment_root)
    environment = dict(os.environ)
    if args.model and args.model.startswith('bedrock:'):
        # Keep chat, title generation, and retrieval on the requested provider.
        # This configures the isolated child only; it does not edit credentials.
        environment.pop('OPENAI_API_KEY', None)
        environment.pop('ANTHROPIC_API_KEY', None)
    if args.study_root:
        environment['REPORT_AGENT_STUDY_ROOT'] = str(args.study_root.resolve())
    if args.embedding_profile:
        environment['DB_RAG_EMBEDDING_PROFILE'] = args.embedding_profile
    checkpoint = artifacts / 'runtime/checkpoints.db'
    environment.update(REPORT_AGENT_RUNTIME_ROOT=str(artifacts / 'runtime'), REPORT_AGENT_CHECKPOINT_DB_PATH=str(checkpoint), REPORT_AGENT_STATIC_DIR=str(ROOT / 'frontend/dist'), PYTHONPATH=str(ROOT))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    process = None
    page = None
    thread = None
    result = {'status': 'running', 'mode': scenario, 'provider_stubs': False}

    def timeout(*_):
        raise TimeoutError('Five-minute feature smoke deadline reached')

    def save(name, value):
        (artifacts / name).write_text(json.dumps(value, indent=2, default=lambda value: value.model_dump(mode='json') if hasattr(value, 'model_dump') else repr(value)) + '\n')

    def api(path):
        response = requests.get(url + path, timeout=5)
        response.raise_for_status()
        return response.json()

    def wait_for(kind, *, prior_interrupt_id=None):
        while time.monotonic() < deadline:
            state = api(f'/api/threads/{thread}/state')
            save('latest-state.json', state)
            interrupt = state.get('active_interrupt') or {}
            if interrupt.get('type') == kind and interrupt.get('id') != prior_interrupt_id:
                return state
            if prior_interrupt_id and interrupt.get('type') == 'agent_clarification':
                raise AssertionError(f'Clear scoped feedback unexpectedly requested clarification: {interrupt}')
            if state.get('run', {}).get('state') == 'error':
                raise AssertionError(state)
            page.wait_for_timeout(300)
        raise TimeoutError(f'Expected {kind}')

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(300)
    with (artifacts / 'api.log').open('w') as log:
        try:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'api.app:build_application', '--factory', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
            while time.monotonic() < deadline:
                assert process.poll() is None, 'Backend exited; inspect api.log'
                try:
                    if requests.get(url + '/api/health', timeout=1).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.25)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    page = browser.new_page()
                    page.goto(url, wait_until='networkidle')
                    options = api('/api/runtime/options')
                    save('runtime-options.json', options)
                    if args.embedding_profile:
                        embedding = options['embedding_startup_status']
                        assert embedding['available'], embedding
                        assert embedding['profile_id'] == args.embedding_profile, embedding
                    if args.model:
                        page.get_by_label('Model', exact=True).select_option(args.model)
                        result['requested_model'] = args.model
                    field = page.get_by_label('Ask a question about your dataset!')
                    field.fill('Create a participant-level dataset from study_id nhanes-2017-2018 containing age RIDAGEYR, sex RIAGENDR, and race/ethnicity RIDRETH3 from DEMO_J. Put each variable in a separate review concept. Use SEQN as the participant identifier. No filters or statistical analysis. Present the dataset plan for human review before extraction.')
                    page.get_by_role('button', name='Send', exact=True).click()
                    while time.monotonic() < deadline:
                        items = api('/api/conversations')['items']
                        if items:
                            thread = items[0]['thread_id']
                            break
                        page.wait_for_timeout(200)
                    assert thread
                    review = wait_for('dataset_plan_review')
                    if args.model:
                        assert review['model_name'] == args.model, review['model_name']
                    save('initial-review.json', review)
                    excluded_column = 'RIAGENDR'
                    if scenario == 'revision_scope':
                        group = review['active_interrupt']['view']['concept_groups'][0]
                        assert len(group['columns']) == 1
                        excluded_column = group['columns'][0]['column']
                        page.get_by_role('textbox', name='Revision feedback', exact=True).fill("don't choose this")
                        page.get_by_role('button', name='Request revision', exact=True).click()
                        revised_review = wait_for('dataset_plan_review', prior_interrupt_id=review['active_interrupt']['id'])
                        save('revised-review.json', revised_review)
                        revised_columns = [column['column'] for concept in revised_review['active_interrupt']['view']['concept_groups'] for column in concept['columns']]
                        assert excluded_column not in revised_columns, revised_columns
                        result['scoped_concept_id'] = group['concept_id']
                    if scenario == 'concept_skip':
                        group = review['active_interrupt']['view']['concept_groups'][0]
                        assert len(group['columns']) == 1 and len(review['active_interrupt']['view']['concept_groups']) > 1
                        excluded_column = group['columns'][0]['column']
                        checkbox_name = f"{group['columns'][0]['table']} · {excluded_column}"
                        page.get_by_role('checkbox', name=checkbox_name, exact=True).uncheck()
                        assert page.get_by_role('button', name='Approve & continue', exact=True).is_enabled()
                        page.get_by_role('button', name='Approve & continue', exact=True).click()
                        page.get_by_role('button', name='Previous concept', exact=True).click()
                        assert not page.get_by_role('checkbox', name=checkbox_name, exact=True).is_checked()
                    page.get_by_role('button', name='All concepts', exact=True).click()
                    excluded = page.get_by_role('checkbox', name=f'DEMO_J · {excluded_column}', exact=True)
                    if scenario == 'revision_scope':
                        assert excluded.count() == 0
                    elif scenario != 'concept_skip':
                        excluded.uncheck()
                    if scenario != 'revision_scope':
                        assert not excluded.is_checked()
                    page.get_by_role('button', name='Approve plan and extract', exact=True).click()
                    dataset_state = wait_for('dataset_review')
                    save('dataset-review.json', dataset_state)
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saved = saver.get_tuple(graph_config(thread))
                    assert saved is not None
                    state = saved.checkpoint['channel_values']
                    state = state.get('__root__', state)
                    save('checkpoint.json', state)
                    store = StateArtifactStore.from_state(state)
                    approved = [item for item in store.list_artifacts(kind='dataset_plan') if item.status == 'approved']
                    assert len(approved) == 1
                    selected = [field for concept in approved[0].content['concepts'] for field in concept['fields']]
                    assert excluded_column not in [field['column'] for field in selected]
                    datasets = list(state['artifacts']['datasets'].values())
                    assert len(datasets) == 1
                    columns = pd.read_parquet(datasets[0]['path']).columns.tolist()
                    schema = json.loads(Path(datasets[0]['schema_path']).read_text())
                    assert excluded_column not in json.dumps(schema), schema
                    assert len(columns) == len(selected) + len(approved[0].content['required_fields'])
                    result.update(status='passed', thread_id=thread, output_columns=columns, excluded=f'DEMO_J.{excluded_column}')
                    page.screenshot(path=str(artifacts / 'success.png'), full_page=True)
                finally:
                    if page:
                        (artifacts / 'page.txt').write_text(page.locator('body').inner_text())
                        (artifacts / 'page.html').write_text(page.content())
                        page.screenshot(path=str(artifacts / 'page.png'), full_page=True)
                    browser.close()
        except BaseException:
            result.update(status='failed', traceback=traceback.format_exc())
            if thread:
                try: save('failure-state.json', api(f'/api/threads/{thread}/state'))
                except Exception: pass
                try:
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saved = saver.get_tuple(graph_config(thread))
                    if saved:
                        save('failure-checkpoint.json', saved.checkpoint)
                except Exception: pass
            raise
        finally:
            if process:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill()
            signal.alarm(0)
            result['seconds'] = time.monotonic() - started
            save('result.json', result)
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
