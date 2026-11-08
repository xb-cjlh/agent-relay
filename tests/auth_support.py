"""Isolated SMTP delivery and cookie client; no external email or user databases."""
import asyncore
import smtpd
import threading
import re
import time
import json
import http.cookiejar
import urllib.request
import urllib.error
from email import policy
from email.parser import BytesParser
from scripts.devtools import APIError


class Mailbox:
    def __init__(self):
        self.messages = []
        self.map = {}
        parent = self
        class Sink(smtpd.SMTPServer):
            def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
                message = BytesParser(policy=policy.default).parsebytes(data)
                parent.messages.append((rcpttos, message.get_content()))
        self.server = Sink(('127.0.0.1', 0), None, map=self.map, decode_data=False)
        self.port = self.server.socket.getsockname()[1]
        self.stopping = threading.Event()
        def run():
            while not self.stopping.is_set():asyncore.loop(timeout=.1, map=self.map, count=1)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def environment(self):
        return {'SPRING_MAIL_HOST': '127.0.0.1', 'SPRING_MAIL_PORT': str(self.port),
                'RELAY_MAIL_FROM': 'relay@test.invalid', 'SPRING_MAIL_PROPERTIES_MAIL_SMTP_AUTH': 'false',
                'SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_ENABLE': 'false',
                'SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_REQUIRED': 'false'}

    def code(self, email):
        for recipients, text in reversed(self.messages):
            if email in recipients:
                return re.search(r'\b([0-9]{6})\b', text)[1]
        raise AssertionError('No verification email received')

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)
        self.server.close()
        for value in list(self.map.values()): value.close()


class BrowserClient:
    def __init__(self, server):
        self.server = server
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = {}

    def request(self, method, path, body=None, raw=False, headers=None):
        request = urllib.request.Request(self.server+path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type':'application/json', 'X-Relay-Request':'1', **self.headers, **(headers or {})})
        try:
            with self.opener.open(request, timeout=20) as response:
                data=response.read()
                return (data, response.headers) if raw else json.loads(data)
        except urllib.error.HTTPError as error:
            raise APIError(json.loads(error.read()).get('error',str(error)),error.code) from None

    def register(self, mailbox, email, password='Relay-password-2026'):
        self.request('POST','/api/auth/email-code',{'email':email,'purpose':'register'})
        return self.request('POST','/api/auth/register',{'email':email,'password':password,'code':mailbox.code(email)})
