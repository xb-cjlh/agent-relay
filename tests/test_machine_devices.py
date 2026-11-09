"""Machine identity, unrestricted discovery, and lossless legacy adoption."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import test_relay as fixtures
from scripts.devtools import APIError, Client, binary
from test_sessions import codex_records, write_records


class MachineDevicesCase(unittest.TestCase):
    setUp = fixtures.ServerCase.setUp
    tearDown = fixtures.ServerCase.tearDown
    register = fixtures.ServerCase.register
    create_agent = fixtures.ServerCase.create_agent
    prompt = fixtures.ServerCase.prompt

    def machine(self, name, fingerprint, roots=None):
        identity = self.client.request('POST', '/api/register', {
            'name': name, 'platform': 'test', 'machine_id': fingerprint,
            'roots': [str(self.root)] if roots is None else roots,
            'capabilities': {'unlimited_tasks': True, 'codex': True, 'claude': True},
        })
        return identity, Client(self.url, identity['token'])

    def session(self, sid='native-1', **values):
        return dict(kind='codex', session_id=sid, name=sid, cwd=str(self.root),
                    busy=False, messages=[dict(id=sid + ':1', role='user', content='history', created_at=1)], **values)

    def assert_conflict(self, action):
        with self.assertRaises(APIError) as error:
            action()
        self.assertEqual(error.exception.status, 409)

    def test_unique_machine_identity_and_legacy_binding(self):
        identity, device = self.machine('one', 'a' * 64)
        self.assert_conflict(lambda: self.machine('different-directory', 'a' * 64))
        _, legacy = self.register('old')
        self.assert_conflict(lambda: legacy.request('POST', '/api/device/poll', {'machine_id': 'a' * 64}))
        self.assert_conflict(lambda: device.request('POST', '/api/device/poll', {'machine_id': 'b' * 64}))
        legacy.request('POST', '/api/device/poll', {'machine_id': 'b' * 64})
        state = self.client.request('GET', '/api/state')
        self.assertEqual({d['machine_id'] for d in state['devices']}, {'a' * 64, 'b' * 64})
        self.assertEqual(next(d['id'] for d in state['devices'] if d['name'] == 'one'), identity['id'])

    def test_rootless_access_and_unavailable_working_directory(self):
        identity, device = self.machine('whole-machine', 'a' * 64, roots=[])
        session = self.session(controllable=True, control_reason='')
        device.request('POST', '/api/device/poll', {'sessions': [session]})
        agent = self.client.request('GET', '/api/state')['agents'][0]
        self.assertTrue(agent['controllable'])
        self.prompt(agent)
        self.client.request('POST', '/api/jobs', {'kind': 'file_read', 'device': identity['id'], 'path': '/tmp/file'})
        # Missing directories still prevent resuming, independently of legacy roots.
        device.request('POST', '/api/device/poll', {'sessions': [dict(session, controllable=False, control_reason='cwd_missing')]})
        self.assert_conflict(lambda: self.prompt(agent))
        reply = device.request('POST', '/api/device/poll', {'capacity': 1})
        prompt_job=next(j for j in self.client.request('GET','/api/state')['jobs'] if j['kind']=='prompt')
        self.assertEqual(prompt_job['status'],'failed')
        self.assertEqual(reply['jobs'][0]['kind'],'file_read')

    def test_upgrade_removes_old_directory_restrictions_without_rescanning(self):
        identity, device = self.machine('upgrading', 'a' * 64)
        session = self.session(controllable=False, control_reason='outside_roots')
        device.request('POST', '/api/device/poll', {'sessions': [session]})
        agent = self.client.request('GET', '/api/state')['agents'][0]
        self.assert_conflict(lambda: self.prompt(agent))
        device.request('POST', '/api/device/poll', {'capabilities': {'unlimited_tasks': True, 'codex': True, 'access_scope': 'user'}})
        upgraded = self.client.request('GET', '/api/state')['agents'][0]
        self.assertEqual(upgraded['id'], agent['id'])
        self.assertTrue(upgraded['controllable'])
        self.assertEqual(upgraded['control_reason'], '')
        job = self.prompt(upgraded)
        claimed = device.request('POST', '/api/device/poll', {'capacity': 1})['jobs'][0]
        self.assertEqual(claimed['id'], job['id'])
        self.assertEqual(claimed['agent']['session_id'], session['session_id'])
        device.request('POST', '/api/device/poll', {'sessions': [dict(session, control_reason='cwd_missing')]})
        self.assertFalse(self.client.request('GET', '/api/state')['agents'][0]['controllable'])

    def test_merge_preserves_history_files_and_message_deduplication(self):
        target, target_client = self.machine('target', 'a' * 64)
        source, source_client = self.register('old')
        session = self.session()
        source_client.request('POST', '/api/device/poll', {'sessions': [session]})
        agent = self.client.request('GET', '/api/state')['agents'][0]
        job = self.prompt(agent, 'legacy prompt')
        file_job = self.client.request('POST', '/api/jobs', {'kind': 'file_read', 'device': source['id'], 'path': 'XX.txt'})
        source_client.request('POST', '/api/device/poll', {'capacity': 2})
        source_client.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'output': 'legacy answer'})
        data = b'old file'
        source_client.request('POST', '/api/device/jobs/' + file_job['id'] + '/complete', {'status': 'succeeded',
            'file': {'name': 'XX.txt', 'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}})
        plan = dict(target=target['id'], sources=[source['id']])
        preview = self.client.request('POST', '/api/devices/merge', dict(plan, dry_run=True))
        self.assertEqual(preview['sources'][0]['agents'], 1)
        self.assertEqual(len(self.client.request('GET', '/api/state')['devices']), 2)
        self.client.request('POST', '/api/devices/merge', plan)
        state = self.client.request('GET', '/api/state')
        self.assertEqual(len(state['devices']), 1)
        self.assertEqual(state['agents'][0]['id'], agent['id'])
        self.assertEqual(state['agents'][0]['device_id'], target['id'])
        self.assertEqual(self.client.request('GET', '/api/files/' + state['artifacts'][0]['id'], raw=True)[0], data)
        self.assertEqual(self.client.request('GET', '/api/jobs/' + job['id'])['output'], 'legacy answer')
        with self.assertRaises(APIError) as error:
            source_client.request('POST', '/api/device/poll', {})
        self.assertEqual(error.exception.status, 401)
        before = self.client.request('GET', '/api/agents/' + agent['id'] + '/messages')['messages']
        target_client.request('POST', '/api/device/poll', {'sessions': [session]})
        after = self.client.request('GET', '/api/agents/' + agent['id'] + '/messages')['messages']
        self.assertEqual(before, after)
        self.assertEqual(self.client.request('POST', '/api/devices/merge', plan)['sources'], [])

    def test_merge_rejects_conflicts_before_moving_any_history(self):
        target, _ = self.machine('target', 'a' * 64)
        other, _ = self.machine('other-machine', 'b' * 64)
        self.assert_conflict(lambda: self.client.request('POST', '/api/devices/merge', {'target': target['id'], 'sources': [other['id']]}))
        one, first = self.register('first')
        two, second = self.register('second')
        first.request('POST', '/api/device/poll', {'sessions': [self.session()]})
        second.request('POST', '/api/device/poll', {'sessions': [self.session()]})
        plan=self.client.request('POST', '/api/devices/merge', {'target': target['id'], 'sources': [one['id'], two['id']], 'dry_run': True})
        self.assertEqual(sum(s['coalesced_sessions'] for s in plan['sources']), 1)
        self.assertEqual(len(self.client.request('GET', '/api/state')['devices']), 4)
        agent = next(a for a in self.client.request('GET', '/api/state')['agents'] if a['device_id'] == one['id'])
        self.prompt(agent)
        self.assert_conflict(lambda: self.client.request('POST', '/api/devices/merge', {'target': target['id'], 'sources': [one['id']]}))

    def test_overlapping_sessions_coalesce_and_old_ids_continue_working(self):
        target, target_client = self.machine('target', 'a' * 64)
        one, first = self.register('first')
        two, second = self.register('second')
        session=self.session()
        for device in [target_client, first, second]:
            device.request('POST', '/api/device/poll', {'sessions': [session]})
        before=self.client.request('GET', '/api/state')
        canonical=next(a for a in before['agents'] if a['device_id']==target['id'])
        old=next(a for a in before['agents'] if a['device_id']==one['id'])
        job=self.prompt(old, 'old managed turn')
        first.request('POST', '/api/device/poll', {'capacity': 1})
        first.request('POST', '/api/device/jobs/'+job['id']+'/complete', {'status':'succeeded','output':'old answer'})
        result=self.client.request('POST','/api/devices/merge',{'target':target['id'],'sources':[one['id'],two['id']]})
        self.assertEqual(sum(s['coalesced_sessions'] for s in result['sources']),2)
        target_client.request('POST','/api/device/poll',{'sessions':[session]})
        state=self.client.request('GET','/api/state')
        self.assertEqual(len(state['agents']),1)
        self.assertEqual(state['agents'][0]['id'],canonical['id'])
        conversation=self.client.request('GET','/api/agents/'+old['id']+'/messages')
        self.assertEqual(conversation['agent']['id'],canonical['id'])
        self.assertEqual([m['content'] for m in conversation['messages']],['history','old managed turn','old answer'])
        new_job=self.prompt(old, 'continue via old id')
        claimed=target_client.request('POST','/api/device/poll',{'capacity':1})['jobs'][0]
        self.assertEqual(claimed['id'],new_job['id'])
        self.assertEqual(claimed['agent']['session_id'],session['session_id'])
        target_client.request('POST','/api/device/jobs/'+new_job['id']+'/complete',{'status':'succeeded','output':'continued'})

    def test_physical_daemon_reuses_identity_and_discovers_other_directories(self):
        bindir = self.root / 'bin'; bindir.mkdir()
        for kind in ['codex', 'claude']:
            executable = bindir / kind
            executable.write_text('#!' + sys.executable + '\nimport json, os\nprint(json.dumps({"type":"thread.started","thread_id":"outside"}))\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"fixture cwd="+os.getcwd()}}))\n'); executable.chmod(0o755)
        authorized = self.root / 'authorized'; authorized.mkdir()
        outside = self.root / 'other-project'; outside.mkdir()
        codex_home = self.root / 'codex'
        write_records(codex_home / 'sessions/inside.jsonl', codex_records(authorized, sid='inside'))
        write_records(codex_home / 'sessions/outside.jsonl', codex_records(outside, sid='outside'))
        env = dict(os.environ, AM_SERVER=self.url, AM_TOKEN=fixtures.TOKEN, AM_CONFIG_DIR=str(self.root / 'config'),
                   CODEX_HOME=str(codex_home), CLAUDE_CONFIG_DIR=str(self.root / 'claude'), PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        def start(name, state, *extra):
            return subprocess.Popen([binary(), 'daemon', 'run', '--name', name,
                '--state-dir', str(self.root / state), '--interval', '0.1', *extra], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        process = start('first-name', 'state-one')
        try:
            end = time.monotonic() + 15
            while True:
                state = self.client.request('GET', '/api/state')
                if len(state['agents']) == 2: break
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), end)
                time.sleep(.1)
            identity = state['devices'][0]['id']
            self.assertEqual({a['session_id']: bool(a['controllable']) for a in state['agents']}, {'inside': True, 'outside': True})
            self.assertEqual(state['devices'][0]['capabilities']['access_scope'], 'user')
            other_agent = next(a for a in state['agents'] if a['session_id'] == 'outside')
            result = self.client.wait(self.prompt(other_agent)['id'], 10)
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertIn('fixture cwd=' + os.path.realpath(outside), result['output'])
            content = b'file outside any configured project'
            source = outside / 'source.bin'; source.write_bytes(content)
            link = authorized / 'link'; link.symlink_to(source)
            for path in [str(source), str(link), os.path.relpath(source, Path.home())]:
                job = self.client.request('POST', '/api/jobs', {'kind': 'file_read', 'device': identity, 'path': path})
                result = self.client.wait(job['id'], 10)
                self.assertEqual(result['status'], 'succeeded', result)
                received, _ = self.client.request('GET', '/api/files/' + result['artifact_id'], raw=True)
                self.assertEqual(received, content)
            duplicate = start('different-name', 'state-two')
            _, stderr = duplicate.communicate(timeout=10)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn('本机已连接', stderr.decode())
            process.terminate(); process.wait(timeout=10); process.stderr.close()
            process = start('renamed', 'state-three', '--root', str(self.root / 'no-longer-exists'))
            end = time.monotonic() + 10
            adopted = self.root / 'state-three/identity.json'
            while not adopted.exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), end)
                time.sleep(.1)
            self.assertEqual(json.loads(adopted.read_text())['id'], identity)
            self.assertEqual(len(self.client.request('GET', '/api/state')['devices']), 1)
        finally:
            if process.poll() is None: process.terminate()
            process.wait(timeout=10); process.stderr.close()
