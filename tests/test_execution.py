import json
import unittest

from scripts.devtools import APIError
import test_relay as fixtures


class ExecutionCase(unittest.TestCase):
    setUp = fixtures.ServerCase.setUp
    tearDown = fixtures.ServerCase.tearDown
    register = fixtures.ServerCase.register
    create_agent = fixtures.ServerCase.create_agent
    prompt = fixtures.ServerCase.prompt

    def test_default_has_no_execution_deadline_and_explicit_limits_are_retained(self):
        self.register()
        agent = self.create_agent()
        self.assertEqual(0, self.prompt(agent)['payload']['timeout'])
        body = {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'fixture'}
        for timeout in (0, 1, 1800):
            result = self.client.request('POST', '/api/jobs', {**body, 'timeout': timeout})
            self.assertEqual(timeout, result['payload']['timeout'])
        for invalid in (-1, 1801, True, None):
            with self.assertRaises(APIError) as error:
                self.client.request('POST', '/api/jobs', {**body, 'timeout': invalid})
            self.assertEqual(400, error.exception.status)

    def test_old_daemons_cannot_silently_apply_their_five_minute_default(self):
        _, device = self.register()
        agent = self.create_agent()
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True}})
        with self.assertRaises(APIError) as error:
            self.prompt(agent)
        self.assertEqual(409, error.exception.status)
        self.assertIn('0.6.1', str(error.exception))
        # Explicit positive timeouts remain usable on older daemons.
        positive = self.client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'fixture', 'timeout': 600})
        self.assertEqual(600, positive['payload']['timeout'])
        self.client.request('POST', '/api/jobs/' + positive['id'] + '/cancel', {})
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True, 'unlimited_tasks': True}})
        unlimited = self.prompt(agent)
        # A daemon downgrade after queuing cannot execute with a different limit.
        claimed = device.request('POST', '/api/device/poll', {'capacity': 1, 'capabilities': {'codex': True}})
        self.assertEqual([], claimed['jobs'])
        result = self.client.request('GET', '/api/jobs/' + unlimited['id'])
        self.assertEqual('failed', result['status'])
        self.assertIn('本次指令未执行', result['error'])

    def test_diagnostics_are_separate_during_streaming_and_after_completion(self):
        _, device = self.register()
        agent = self.create_agent()
        job = self.prompt(agent)
        device.request('POST', '/api/device/poll', {'capacity': 1})
        diagnostic = 'OutputTextDelta without active item <fixture>'
        progress = {'id': job['id'], 'output': 'Assistant answer', 'diagnostics': diagnostic}
        device.request('POST', '/api/device/poll', {'active': [progress]})
        path = '/api/agents/' + agent['id'] + '/messages'
        reply = self.client.request('GET', path)['messages'][-1]
        self.assertEqual('Assistant answer', reply['content'])
        self.assertEqual(diagnostic, reply['diagnostics'])
        device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {**progress, 'status': 'succeeded'})
        reply = self.client.request('GET', path)['messages'][-1]
        self.assertEqual('succeeded', reply['status'])
        self.assertEqual('Assistant answer', reply['content'])
        self.assertEqual(diagnostic, reply['diagnostics'])

    def test_native_messages_during_running_jobs_are_only_hidden_when_covered(self):
        _, device = self.register()
        for kind in ['codex', 'claude']:
            agent = self.create_agent(name=kind + '-live', kind=kind)
            job = self.prompt(agent, 'relay question')
            device.request('POST', '/api/device/poll', {'capacity': 1})
            self.server.database.execute('UPDATE jobs SET started_at=1000 WHERE id=?', (job['id'],))
            session = {'kind': kind, 'session_id': kind + '-live-session', 'name': kind,
                       'cwd': str(self.root), 'busy': True, 'updated_at': 1005,
                       'messages': [
                           {'id': kind + '-echo-user', 'role': 'user', 'content': 'relay question', 'created_at': 1001},
                           {'id': kind + '-echo-answer', 'role': 'assistant', 'content': 'first streamed update', 'created_at': 1002},
                           {'id': kind + '-new-answer', 'role': 'assistant', 'content': 'new native reply', 'created_at': 1003},
                           {'id': kind + '-local-user', 'role': 'user', 'content': 'local followup', 'created_at': 1004},
                           {'id': kind + '-local-answer', 'role': 'assistant', 'content': 'streamed', 'created_at': 1005}]}
            device.request('POST', '/api/device/poll', {
                'active': [{'id': job['id'], 'session_id': session['session_id'], 'output': 'first streamed update\n'}],
                'sessions': [session]})
            path = '/api/agents/' + agent['id'] + '/messages'
            reply = self.client.request('GET', path)
            contents = [m['content'] for m in reply['messages']]
            self.assertIn('new native reply', contents)
            self.assertIn('local followup', contents)
            self.assertIn('streamed', contents)  # A substring of another message is not a copy.
            self.assertEqual(contents.count('relay question'), 1)
            self.assertNotIn('first streamed update', contents)
            native = next(m for m in reply['messages'] if m['content'] == 'new native reply')
            self.assertEqual(reply['active_jobs'], [{'id': job['id'], 'status': 'running'}])
            queued = self.prompt(agent, 'queued next question')
            device.request('POST', '/api/device/poll', {'active': [{
                'id': job['id'], 'output': 'first streamed update\nnew native reply\n'}]})
            caught_up = self.client.request('GET', path)
            self.assertIn(native['id'], caught_up['suppressed_message_ids'])
            self.assertNotIn(native['id'], [m['id'] for m in caught_up['messages']])
            self.assertIn('queued next question', [m['content'] for m in caught_up['messages']])
            device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {
                'status': 'succeeded', 'output': 'first streamed update\n'})
            completed = self.client.request('GET', path)
            self.assertIn('new native reply', [m['content'] for m in completed['messages']])
            self.client.request('POST', '/api/jobs/' + queued['id'] + '/cancel', {})

    def test_user_echoes_allow_clock_skew_without_collapsing_repeated_questions(self):
        _, device = self.register()
        for kind in ['codex', 'claude']:
            agent = self.create_agent(name=kind + '-clock-skew', kind=kind)
            first = self.prompt(agent, 'same question')
            device.request('POST', '/api/device/poll', {'capacity': 1})
            self.server.database.execute('UPDATE jobs SET created_at=999.4,started_at=1000 WHERE id=?', (first['id'],))
            session = {'kind': kind, 'session_id': kind + '-clock-session', 'name': kind,
                       'cwd': str(self.root), 'busy': False, 'updated_at': 1005,
                       'messages': [
                           {'id': kind + '-old-question', 'role': 'user', 'content': 'same question', 'created_at': 960},
                           {'id': kind + '-early-echo', 'role': 'user', 'content': 'same question', 'created_at': 999.2},
                           {'id': kind + '-real-repeat', 'role': 'user', 'content': 'same question', 'created_at': 1005}]}
            device.request('POST', '/api/device/poll', {'active': [{
                'id': first['id'], 'session_id': session['session_id'], 'output': 'first answer'}], 'sessions': [session]})
            path = '/api/agents/' + agent['id'] + '/messages'
            reply = self.client.request('GET', path)
            questions = [m for m in reply['messages'] if m['role'] == 'user']
            self.assertEqual(len(questions), 3)  # Old local question, one Relay question, and a real local repeat.
            self.assertEqual(sorted(m['created_at'] for m in questions), [960, 999.4, 1005])
            self.assertEqual(len(reply['suppressed_message_ids']), 1)

            second = self.prompt(agent, 'same question')
            queued = self.client.request('GET', path)
            self.assertEqual(len([m for m in queued['messages'] if m['role'] == 'user']), 4)
            device.request('POST', '/api/device/jobs/' + first['id'] + '/complete', {'status': 'succeeded', 'output': 'first answer'})
            device.request('POST', '/api/device/poll', {'capacity': 1})
            self.server.database.execute('UPDATE jobs SET created_at=1099,started_at=1100 WHERE id=?', (second['id'],))
            session.update(updated_at=1101, messages=[{
                'id': kind + '-second-echo', 'role': 'user', 'content': 'same question', 'created_at': 1101.5}])
            device.request('POST', '/api/device/poll', {'sessions': [session]})
            repeated = self.client.request('GET', path)
            questions = [m for m in repeated['messages'] if m['role'] == 'user']
            self.assertEqual(len(questions), 4)
            self.assertEqual({m['job_id'] for m in questions if m.get('job_id')}, {first['id'], second['id']})
            self.assertEqual(len(repeated['suppressed_message_ids']), 2)
            device.request('POST', '/api/device/jobs/' + second['id'] + '/complete', {'status': 'succeeded', 'output': 'second answer'})

    def test_streamed_assistant_messages_preserve_native_boundaries(self):
        _, device = self.register()
        for kind in ['codex', 'claude']:
            agent = self.create_agent(name=kind + '-boundaries', kind=kind)
            job = self.prompt(agent)
            device.request('POST', '/api/device/poll', {'capacity': 1})
            self.server.database.execute('UPDATE jobs SET started_at=1000 WHERE id=?', (job['id'],))
            first = '先检查项目 👋。\n\n这是同一条消息的第二段。'
            second = '找到结果：\n\n```text\nline one\nline two\n```'
            session = {'kind': kind, 'session_id': kind + '-boundary-session', 'name': kind,
                       'cwd': str(self.root), 'busy': True, 'updated_at': 1004,
                       'messages': [
                           {'id': kind + '-first', 'role': 'assistant', 'content': first, 'created_at': 1001},
                           {'id': kind + '-second', 'role': 'assistant', 'content': second, 'created_at': 1002},
                           {'id': kind + '-repeat', 'role': 'assistant', 'content': first, 'created_at': 1003},
                           {'id': kind + '-independent', 'role': 'assistant', 'content': 'independent native reply', 'created_at': 1004}]}
            output = first + '\n' + second + '\n' + first + '\n还在继续…'
            device.request('POST', '/api/device/poll', {
                'active': [{'id': job['id'], 'session_id': session['session_id'], 'output': output}],
                'sessions': [session]})
            path = '/api/agents/' + agent['id'] + '/messages'

            def parts(reply):
                message = next(m for m in reply['messages'] if m['id'] == job['id'] + ':assistant')
                # Java and JavaScript string offsets use UTF-16 code units.
                text = message['content'].encode('utf-16-le')
                return [text[p['start'] * 2:p['end'] * 2].decode('utf-16-le').strip()
                        for p in message['segments']]

            reply = self.client.request('GET', path)
            self.assertEqual(parts(reply), [first, second, first, '还在继续…'])
            self.assertEqual(len(reply['suppressed_message_ids']), 3)
            self.assertIn('independent native reply', [m['content'] for m in reply['messages']])
            device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'output': output})
            self.assertEqual(parts(self.client.request('GET', path)), [first, second, first, '还在继续…'])

            # Native history may catch up after execution has already finished.
            session.update(messages=[{'id': kind + '-tail', 'role': 'assistant', 'content': '还在继续…', 'created_at': 1005}])
            device.request('POST', '/api/device/poll', {'sessions': [session]})
            caught_up = self.client.request('GET', path)
            self.assertEqual(parts(caught_up), [first, second, first, '还在继续…'])
            self.assertEqual(len(caught_up['suppressed_message_ids']), 4)

    def test_image_user_echoes_match_only_the_attached_images(self):
        _, device = self.register()
        prompt = 'Look at this screenshot\nKeep this literal: <image>example</image>'

        def tag(image, index=1):
            return f'<image name=[Image #{index}] path="/Users/fixture/Relay data/images/{image}">\n</image>'

        for kind, ids in [('codex', ['img_first']), ('codex', ['img_one', 'img_two']), ('claude', ['img_claude'])]:
            agent = self.create_agent(name=kind + '-' + ids[0], kind=kind)
            job = self.prompt(agent, prompt)
            device.request('POST', '/api/device/poll', {'capacity': 1})
            images = [{'id': image, 'mime': 'image/png' if i == 0 else 'image/jpeg'} for i, image in enumerate(ids)]
            payload = {**job['payload'], 'images': images}
            self.server.database.execute('UPDATE jobs SET created_at=999,started_at=1000,payload=? WHERE id=?', (json.dumps(payload), job['id']))
            files = [image['id'] + ('.png' if image['mime'] == 'image/png' else '.jpeg') for image in images]
            suffix = '\n'.join(tag(file, i + 1) for i, file in enumerate(files))
            echo = prompt + '\n' + suffix if kind == 'codex' else prompt
            # A valid echo arriving after other messages must still replace only
            # one copy. Other images, extra text and literal markup remain visible.
            other = prompt + '\n' + tag('img_other.png')
            session = {'kind': kind, 'session_id': kind + '-' + ids[0], 'name': kind,
                       'cwd': str(self.root), 'busy': True, 'updated_at': 1005,
                       'messages': [
                           {'id': 'echo', 'role': 'user', 'content': echo, 'created_at': 998.35},
                           {'id': 'real-repeat', 'role': 'user', 'content': echo, 'created_at': 1005},
                           {'id': 'other-image', 'role': 'user', 'content': other, 'created_at': 1000},
                           {'id': 'extra-text', 'role': 'user', 'content': prompt + '\n' + suffix + '\nextra instruction', 'created_at': 1001},
                           {'id': 'incomplete-images', 'role': 'user', 'content': prompt + '\n' + tag(files[0]) + '\n<image>keep me</image>', 'created_at': 1002}]}
            if kind == 'codex':
                session['messages'].append({'id': 'text-only-repeat', 'role': 'user', 'content': prompt, 'created_at': 1000})
            if len(files) == 2:
                session['messages'].extend([
                    {'id': 'missing-image', 'role': 'user', 'content': prompt + '\n' + tag(files[0]), 'created_at': 1000},
                    {'id': 'reordered-images', 'role': 'user', 'content': prompt + '\n' + tag(files[1]) + '\n' + tag(files[0], 2), 'created_at': 1000}])
            expected_questions = len(session['messages'])
            for message in session['messages']:
                message['id'] = agent['id'] + '-' + message['id']
            device.request('POST', '/api/device/poll', {'active': [{'id': job['id'], 'session_id': session['session_id'], 'output': ''}], 'sessions': [session]})
            path = '/api/agents/' + agent['id'] + '/messages'
            reply = self.client.request('GET', path)
            questions = [m for m in reply['messages'] if m['role'] == 'user']
            self.assertEqual(len(questions), expected_questions)
            self.assertEqual(len(reply['suppressed_message_ids']), 1)
            canonical = next(m for m in questions if m['id'] == job['id'] + ':user')
            self.assertEqual(canonical['content'], prompt)
            self.assertEqual(canonical['images'], images)
            if kind == 'codex':
                self.assertTrue(any(m['content'] == prompt and not m.get('job_id') for m in questions))
            self.assertIn(other, [m['content'] for m in questions])
            self.assertEqual(sum(m['content'] == echo and not m.get('job_id') for m in questions), 1)
            # Queued sends never consume another copy, even with the same images.
            queued = self.prompt(agent, prompt)
            self.server.database.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload), queued['id']))
            self.assertEqual(len(self.client.request('GET', path)['suppressed_message_ids']), 1)
            device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'output': 'done'})
            self.assertEqual(len(self.client.request('GET', path)['suppressed_message_ids']), 1)
            self.client.request('POST', '/api/jobs/' + queued['id'] + '/cancel', {})

        # Image-like user text without an attachment is never normalized away.
        agent = self.create_agent(name='no-images')
        job = self.prompt(agent, prompt)
        device.request('POST', '/api/device/poll', {'capacity': 1})
        session.update(kind='codex', session_id='no-images', messages=[{
            'id': 'literal-image', 'role': 'user', 'content': prompt + '\n' + tag('img_first.png'), 'created_at': 1000}])
        self.server.database.execute('UPDATE jobs SET started_at=1000 WHERE id=?', (job['id'],))
        device.request('POST', '/api/device/poll', {'active': [{'id': job['id'], 'session_id': 'no-images', 'output': ''}], 'sessions': [session]})
        reply = self.client.request('GET', '/api/agents/' + agent['id'] + '/messages')
        self.assertEqual(len([m for m in reply['messages'] if m['role'] == 'user']), 2)
        self.assertEqual(reply['suppressed_message_ids'], [])
