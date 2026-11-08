#!/usr/bin/env python3.11
"""API 全端点冒烟：每条路由至少打一次，5xx 即为 bug（4xx 合法）。"""
import json
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, '/workspace/pocketpanel-python')
import app  # noqa: E402

BASE = 'http://127.0.0.1:8901'
PASS = 'dcqjbycdc5'

# 会真的下载二进制 / 改系统目录 / 起进程的路由，冒烟不碰
# 注意：路由 pattern 末尾带字面 `$`，正则里要写成 `\$$`
SKIP_RE = re.compile(
    r'/install\$$|/uninstall\$$|/install/speedtest\$$'
    r'|/(start|stop|restart)\$$')

# 参数化路由的示例值：按捕获组内容选
def fill_path(pattern):
    def repl(m):
        g = m.group(0)
        if 'kill' in g:
            return 'kill'
        if '\\d' in g:
            return '999999'
        if 'start' in g:
            return 'stop'
        return 'tpx'
    return re.sub(r'\([^)]*\)', repl, pattern).lstrip('^').rstrip('$')

SAMPLE_BODY = {
    '/api/login': {'username': 'admin', 'password': PASS},
    '/api/passwd': {},
    '/api/file/mkdir': {'path': '/data/data/com.termux/files/home', 'name': 'x'},
    '/api/file/touch': {'path': '/data/data/com.termux/files/home', 'name': 'x'},
    '/api/file/rename': {'path': '/x', 'name': 'y'},
    '/api/file/copy': {'path': '/x', 'to': '/y'},
    '/api/file/chmod': {'path': '/x', 'mode': '644'},
    '/api/search': {'path': '/', 'q': 'x'},
    '/api/archive': {'paths': [], 'fmt': 'zip'},
    '/api/extract': {'path': '/x.zip'},
    '/api/download/remote': {'url': 'http://127.0.0.1:1/x', 'dir': '/tmp'},
    '/api/exec': {'cmd': 'true'},
    '/api/term/new': {'kind': 'local', 'shell': 'bash', 'cols': 80, 'rows': 24},
    '/api/term/tpnone/input': {'data': 'aGk='},
    '/api/term/tpnone/resize': {'cols': 80, 'rows': 24},
    '/api/term/tpnone/signal': {'sig': 'term'},
    '/api/ssh/hosts': {'name': 'x', 'host': '127.0.0.1'},
    '/api/ssh/hosts/delete': {'name': 'x'},
    '/api/ssh/keys': {'name': 'k', 'key': 'x'},
    '/api/schedule': {'name': 'x', 'expr': '* * * * *', 'cmd': 'true'},
    '/api/schedule/tpnone': {'name': 'x'},
    '/api/schedule/tpnone/delete': {},
    '/api/schedule/tpnone/run': {},
    '/api/schedule/tpnone/toggle': {},
    '/api/schedule/preview': {'expr': '* * * * *', 'type': 'shell', 'params': {}},
    '/api/tasktype/shell/dryrun': {'params': {'cmd': 'true'}},
    '/api/messages/read': {'all': True},
    '/api/messages/clear': {},
    '/api/upload/init': {'path': '/tmp', 'name': 'x', 'size': 1, 'chunk_size': 10},
    '/api/upload/tid/0': {},
    '/api/upload/tid/complete': {},
    '/api/upload/tid/abort': {},
    '/api/shares': {'path': '/x'},
    '/api/shares/revoke': {'id': 'x'},
    '/api/trash/restore': {'id': 'x'},
    '/api/trash/purge': {'id': 'x'},
    '/api/versions/restore': {'id': 'x'},
    '/api/versions/delete': {'id': 'x'},
    '/api/favorites/toggle': {'path': '/x'},
    '/api/tunnel/instance': {'name': 'x', 'text': 'serverAddr = "a"\\nserverPort = 7000\\n\\n[[proxies]]\\nname = "p"\\ntype = "tcp"\\nlocal_port = 1\\nremote_port = 2\\n'},
    '/api/tunnel/instance/tpnone': {'probe_url': ''},
    '/api/tunnel/instance/tpnone/start': {},
    '/api/tunnel/instance/tpnone/stop': {},
    '/api/tunnel/instance/tpnone/restart': {},
    '/api/tunnel/instance/tpnone/delete': {},
    '/api/tunnel/instance/tpnone/probe': {},
    '/api/tunnel/parse': {'text': 'serverAddr = "a"'},
    '/api/tunnel/render': {'struct': {}, 'fmt': 'toml'},
    '/api/tunnel/install': {'source': 'github', 'version': ''},
    '/api/tunnel/install/speedtest': {},
    '/api/tunnel/uninstall': {},
}


def req(method, path, body=None, jar=None):
    r = urllib.request.Request(BASE + path, method=method)
    if jar:
        r.add_header('Cookie', jar)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        r.add_header('Content-Type', 'application/json')
    try:
        resp = urllib.request.urlopen(r, data=data, timeout=25)
    except urllib.error.HTTPError as e:
        resp = e
    sc = resp.headers.get('Set-Cookie')
    return resp.status, (sc.split(';')[0] if sc else None), resp.read()[:200]


def main():
    jar = None
    st, c, _ = req('POST', '/api/login', SAMPLE_BODY['/api/login'])
    assert st == 200, f'login failed {st}'
    jar = c
    st_probe, _, body_probe = req('GET', '/api/tunnel/instances', None, jar)
    print(f'login ok, cookie probe: {st_probe} {body_probe[:60]}')
    if st_probe == 401:
        print('BUG: 登录 cookie 未生效')
        return 1

    seen = set()
    bugs = []
    skipped = []
    for method, rx, fn, need_auth in app.COMPILED:
        if (method, rx.pattern) in seen:
            continue
        if method is None:
            continue  # dispatch 型路由（file/upload）由具体端点覆盖
        if rx.pattern == '^/api/logout$':
            continue  # 会注销当前会话，放最后单独验证
        if SKIP_RE.search(rx.pattern):
            # 安装/卸载类路由会真的下载二进制、改系统目录 —— 冒烟不该有这种副作用
            skipped.append(f'{method} {rx.pattern}')
            continue
        seen.add((method, rx.pattern))
        path = fill_path(rx.pattern)
        body = SAMPLE_BODY.get(rx.pattern.lstrip('^').rstrip('$'))
        if method == 'GET' and body:
            body = None
        # 未登录（不带 cookie）打需要鉴权的路由 → 应 401
        st0, _, _ = req(method, path, body)
        if need_auth and st0 != 401:
            bugs.append(f'鉴权缺失: {method} {rx.pattern} 未登录返回 {st0}')
        # 登录后打 → 不应 5xx
        st1, _, body1 = req(method, path, body, jar)
        if st1 >= 500:
            bugs.append(f'5xx: {method} {rx.pattern} → {st1} {body1[:120]}')
        print(f'  {st0}/{st1}  {method:6} {rx.pattern}')

    # file_dispatch / upload_route 的具体端点已在上面的表里覆盖大部分，
    # 再补 file GET/PUT/DELETE 与 upload PUT
    for m, p in [('GET', '/api/file?path=/nonexist'), ('DELETE', '/api/file?path=/nonexist'),
                 ('PUT', '/api/file'), ('GET', '/api/upload/tid')]:
        st1, _, body1 = req(m, p, {'path': '/nonexist'} if m == 'PUT' else None, jar)
        if st1 >= 500:
            bugs.append(f'5xx: {m} {p} → {st1} {body1[:120]}')
        print(f'  {st1}  {m:6} {p}')

    # logout 放最后：未登录 401、登录后 200（并真实注销）
    st0, _, _ = req('POST', '/api/logout', {})
    if st0 != 401:
        bugs.append(f'logout 未登录应 401，实际 {st0}')
    st1, _, _ = req('POST', '/api/logout', {}, jar)
    if st1 != 200:
        bugs.append(f'logout 登录后应 200，实际 {st1}')
    st2, _, _ = req('GET', '/api/session', None, jar)
    print(f'  logout: {st0}/{st1}，注销后 session={st2}')

    print(f'\n== 冒烟 {len(seen)+4} 条，bug {len(bugs)} 个')
    if skipped:
        print(f'  （跳过 {len(skipped)} 条有真实副作用的安装类路由）')
        for s in skipped:
            print('   skip', s)
    for b in bugs:
        print(' BUG', b)
    return 1 if bugs else 0


if __name__ == '__main__':
    sys.exit(main())
