"""Busy input delivery without relaxing old clients or serializing file jobs."""
import time
import unittest
from scripts.devtools import Client
import test_relay


class CodexSteeringCase(unittest.TestCase):
    setUp = test_relay.ServerCase.setUp
    tearDown = test_relay.ServerCase.tearDown
    prompt = test_relay.ServerCase.prompt

    def prepare(self, kind='codex', busy=True, shared=True):
        identity = self.client.request('POST', '/api/register', {
            'name': 'steering-device', 'platform': 'test', 'roots': [str(self.root)],
            'capabilities': {'codex': True, 'claude': True, 'unlimited_tasks': True, 'codex_steering': shared},
        })
        device = Client(self.url, identity['token'])
        session = {'kind': kind, 'session_id': 'existing-session', 'name': 'native',
                   'cwd': str(self.root), 'busy': busy, 'updated_at': time.time()}
        device.request('POST', '/api/device/poll', {'capacity': 0, 'sessions': [session]})
        agent = self.client.request('GET', '/api/state')['agents'][0]
        return device, session, agent

    def test_native_busy_input_delivers_in_order_and_remains_busy(self):
        device, session, agent = self.prepare()
        first, second = self.prompt(agent, 'first update'), self.prompt(agent, 'second update')
        jobs = device.request('POST', '/api/device/poll', {'capacity': 4})['jobs']
        self.assertEqual([j['id'] for j in jobs], [first['id']])
        self.assertTrue(jobs[0]['steer_only'])
        self.assertEqual(device.request('POST', '/api/device/poll', {'capacity': 4})['jobs'], [])
        device.request('POST', '/api/device/jobs/' + first['id'] + '/complete', {
            'status': 'succeeded', 'steered': True, 'session_id': session['session_id'], 'output': ''})
        state = self.client.request('GET', '/api/state')
        self.assertTrue(state['agents'][0]['external_busy'])
        self.assertTrue(state['agents'][0]['busy'])
        jobs = device.request('POST', '/api/device/poll', {'capacity': 4})['jobs']
        self.assertEqual([j['id'] for j in jobs], [second['id']])
        self.assertTrue(jobs[0]['steer_only'])

    def test_relay_owned_input_uses_reserved_slots_and_keeps_owner_running(self):
        device, session, agent = self.prepare(busy=False, shared=False)
        original = self.prompt(agent, 'original task')
        device.request('POST', '/api/device/poll', {'capacity': 1})
        extra = self.prompt(agent, 'additional instruction')
        active = [{'id': original['id'], 'session_id': session['session_id'], 'steerable': True}]
        jobs = device.request('POST', '/api/device/poll', {
            'capacity': 0, 'steer_capacity': 1, 'active': active})['jobs']
        self.assertEqual([j['id'] for j in jobs], [extra['id']])
        self.assertTrue(jobs[0]['steer_only'])
        device.request('POST', '/api/device/jobs/' + extra['id'] + '/complete', {
            'status': 'succeeded', 'steered': True, 'output': ''})
        self.assertEqual(self.client.request('GET', '/api/jobs/' + original['id'])['status'], 'running')
        messages = self.client.request('GET', '/api/agents/' + agent['id'] + '/messages')['messages']
        self.assertEqual([m['content'] for m in messages if m['role']=='user'], ['original task', 'additional instruction'])
        self.assertFalse(any(m['content'] for m in messages if m['role']=='assistant'))

    def test_failed_delivery_does_not_mark_the_native_turn_finished(self):
        device, session, agent = self.prepare()
        extra = self.prompt(agent, 'a failed transport')
        device.request('POST', '/api/device/poll', {'capacity': 1})
        device.request('POST', '/api/device/jobs/' + extra['id'] + '/complete', {
            'status': 'failed', 'steering': True, 'error': 'fixture connection closed'})
        self.assertTrue(self.client.request('GET', '/api/state')['agents'][0]['external_busy'])

    def test_standalone_running_jobs_and_claude_stay_serialized(self):
        device, session, agent = self.prepare(busy=False)
        original = self.prompt(agent)
        device.request('POST', '/api/device/poll', {'capacity': 1})
        self.prompt(agent, 'wait for legacy exec')
        self.assertEqual(device.request('POST', '/api/device/poll', {
            'capacity': 4, 'steer_capacity': 4, 'active': [{'id': original['id'], 'steerable': False}]})['jobs'], [])
        claude = self.client.request('POST', '/api/agents', {
            'device': 'steering-device', 'kind': 'claude', 'name': 'Claude', 'cwd': str(self.root)})
        running = self.prompt(claude)
        device.request('POST', '/api/device/poll', {'capacity': 1})
        self.prompt(claude, 'wait for Claude')
        self.assertEqual(device.request('POST', '/api/device/poll', {
            'capacity': 4, 'steer_capacity': 4, 'active': [{'id': running['id'], 'steerable': True}]})['jobs'], [])
