"""Real MySQL test databases, separate from the shared development application data."""
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def settings():
    path = Path(os.environ.get('RELAY_MYSQL_TEST_CONFIG', str(PROJECT / '.local/mysql-test.json')))
    if not path.exists():
        raise RuntimeError('Configure .local/mysql-test.json for the MySQL integration tests; see README.')
    value = json.loads(path.read_text())
    if value.get('host', '127.0.0.1') not in ('127.0.0.1', 'localhost'):
        raise RuntimeError('Destructive test database setup is restricted to local MySQL.')
    if 'env_file' in value:
        env = dict(line.split('=', 1) for line in Path(value['env_file']).read_text().splitlines()
                   if line.strip() and not line.startswith('#') and '=' in line)
        value['password'] = env['MYSQL_ROOT_PASSWORD']
    return value


class Database:
    def __init__(self):
        self.settings = settings()
        self.name = 'relay_test_' + secrets.token_hex(8)
        self.execute('CREATE DATABASE `' + self.name + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin', use_database=False)

    def execute(self, sql, args=(), use_database=True):
        def literal(value):
            if value is None:
                return 'NULL'
            if isinstance(value, (int, float)):
                return repr(value)
            return "CONVERT(X'" + str(value).encode().hex() + "' USING utf8mb4)"
        for value in args:
            sql = sql.replace('?', literal(value), 1)
        config = self.settings
        argv = ['mysql', '--host', config.get('host', '127.0.0.1'), '--port', str(config.get('port', 13306)),
                '--user', config.get('user', 'root'), '--batch', '--raw']
        if use_database:
            argv += ['--database', self.name]
        process = subprocess.run(argv, input=sql, capture_output=True, text=True,
            env=dict(os.environ, MYSQL_PWD=config['password']), timeout=30)
        if process.returncode:
            raise RuntimeError('MySQL test operation failed: ' + process.stderr)
        return process.stdout

    def environment(self):
        c = self.settings
        return {'RELAY_DB_URL': 'jdbc:mysql://%s:%s/%s?useUnicode=true&characterEncoding=UTF-8&serverTimezone=UTC&connectTimeout=5000&socketTimeout=20000' %
                (c.get('host', '127.0.0.1'), c.get('port', 13306), self.name),
                'RELAY_DB_USERNAME': c.get('user', 'root'), 'RELAY_DB_PASSWORD': c['password']}

    def close(self):
        if not re.fullmatch('relay_test_[0-9a-f]{16}', self.name):
            raise RuntimeError('Refusing to remove an unrecognized test database')
        self.execute('DROP DATABASE IF EXISTS `' + self.name + '`', use_database=False)
