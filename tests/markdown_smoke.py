"""Conversation Markdown, streaming and untrusted output in Chromium and WebKit."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client, Server
from test_relay import TOKEN
from playwright.sync_api import expect, sync_playwright


MARKDOWN = r'''# 结论

**重点**，*强调*，~~删除~~，`inline **literal**`。

不同壁垒受到的影响，可以这样看：

| 壁垒来源 | AI 带来的变化 | 数量 |
| :--- | :---: | ---: |
| 功能多、实现复杂 | 模仿速度加快，领先窗口缩短 | 1 |
| 专有数据 | 有合法使用权、持续更新的数据更有价值 | 2 |
| `a\|b` | a\|b | 3 |

## 列表

- 第一项
  - 嵌套项
- 第二项

3. 第三项
4. 第四项

> 引用包含 **粗体**。

[Relay 文档](https://relay.example.test/docs?a=1&b=2)

```python
print("<script>literal</script>")
```

~~~shell
echo 'second code block'
~~~

<img src="https://untrusted.example.test/pixel" onerror="window.markdownInjected=true">
<script>window.markdownInjected=true</script>

![external image](https://untrusted.example.test/image.png)

[bad](javascript:alert(1))
[encoded](javascript&#x3a;alert(1))
[data](data:text/html;base64,PHNjcmlwdD4=)
'''

MATH = r'''

行内公式 \(s=\frac12+it\)，以及 $x^2+y^2=z^2$，**重点 \(\zeta(s)=0\)**。

黎曼 ζ 函数：
\[
\zeta(s)=1+\frac1{2^s}+\frac1{3^s}+\cdots
\]

- 非平凡零点位于：
  \[
  \boxed{\operatorname{Re}(s)=\frac12}
  \]

$$
\begin{pmatrix}a & b \\ c & d\end{pmatrix}
$$

\[
''' + '+'.join(r'\frac{1}{'+str(i)+'^s}' for i in range(1,31)) + r'''
\]

价格 $5 和 $10，转义美元 \$20。

代码保持原样：`\(literal\)` 和 `$code$`。

```text
\[code\]
$code$
```

无效公式 \(\frac{\)，后续正文仍然可读。

\(\href{javascript:alert(1)}{bad}\)
\(\includegraphics{https://untrusted.example.test/math.png}\)
\(\htmlStyle{background:url(https://untrusted.example.test/math)}{bad}\)
\(\def\loop{\loop}\loop\)
'''


def assert_math(page, body):
    expect(body.locator('.math-formula')).to_have_count(12)
    expect(body.locator('.math-display')).to_have_count(4)
    expect(body.locator('.math-formula .katex')).to_have_count(10)
    expect(body.locator('.math-fallback')).to_have_count(2)
    expect(body.locator('strong .katex')).to_have_count(1)
    expect(body.locator('code').filter(has_text=r'\(literal\)')).to_have_count(1)
    expect(body).to_contain_text('价格 $5 和 $10，转义美元 $20。')
    assert body.locator('.math-formula a,.math-formula img,.math-formula script,.math-formula [onerror]').count() == 0
    assert body.locator('.math-formula math annotation').first.text_content() == r's=\frac12+it'
    page.evaluate('document.fonts.ready')
    assert page.evaluate("document.fonts.check('16px KaTeX_Main')")
    # CSSOM-built KaTeX positioning must work with the production CSP intact.
    assert body.locator('.katex .vlist > span').first.evaluate('(el)=>getComputedStyle(el).top') != 'auto'
    long = body.locator('.math-display').last
    if page.viewport_size['width'] < 500:
        assert long.evaluate('(el)=>el.scrollWidth>el.clientWidth')
        long.evaluate('(el)=>{el.scrollLeft=100}')
        assert long.evaluate('(el)=>el.scrollLeft') > 0
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        server = Server(('127.0.0.1', 0), root / 'data', TOKEN)
        base = 'http://127.0.0.1:' + str(server.server_port)
        client = Client(base, TOKEN)
        try:
            registration = client.request('POST', '/api/register', {
                'name': 'markdown-fixture', 'platform': 'fixture', 'roots': [str(root)],
                'capabilities': {'codex': True, 'claude': True, 'unlimited_tasks': True}})
            device = Client(base, registration['token'])
            agents = {}
            for kind in ('codex', 'claude'):
                agent = client.request('POST', '/api/agents', {
                    'device': registration['id'], 'kind': kind, 'name': kind, 'cwd': str(root)})
                job = client.request('POST', '/api/jobs', {
                    'kind': 'prompt', 'agent': agent['id'], 'prompt': '# User **literal** <b>tag</b>'})
                device.request('POST', '/api/device/poll', {'capacity': 1})
                device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {
                    'status': 'succeeded', 'output': MARKDOWN + MATH})
                agents[kind] = agent
            with sync_playwright() as p:
                for name, engine in [('Chromium', p.chromium), ('WebKit', p.webkit)]:
                    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                    browser = engine.launch(**({'executable_path': str(chrome)} if name == 'Chromium' and chrome.exists() else {}))
                    context = browser.new_context(**({'viewport': {'width': 390, 'height': 650}, 'is_mobile': True, 'has_touch': True} if name == 'WebKit' else {}))
                    # Check code-copy values without depending on OS clipboard permission.
                    context.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copiedCode=text;}}});")
                    page = context.new_page(); errors = []; external = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' and not message.text.startswith('Viewport argument key "interactive-widget"') else None)
                    context.route('https://untrusted.example.test/**', lambda route: (external.append(route.request.url), route.abort()))
                    page.goto(base); server.browser_login(page)
                    for kind, agent in agents.items():
                        page.locator('[data-agent="' + agent['id'] + '"]').evaluate('(el)=>el.click()')
                        body = page.locator('#messages .assistant .message-body').first
                        expect(body.locator('h1')).to_have_text('结论')
                        expect(body.locator('h2')).to_have_text('列表')
                        expect(body.locator('table')).to_have_count(1)
                        expect(body.locator('th')).to_have_count(3)
                        expect(body.locator('tbody tr')).to_have_count(3)
                        expect(body.locator('tbody tr').last.locator('td').first).to_have_text('a|b')
                        assert body.locator('th').nth(1).evaluate('(el)=>getComputedStyle(el).textAlign') == 'center'
                        assert body.locator('th').nth(2).evaluate('(el)=>getComputedStyle(el).textAlign') == 'right'
                        expect(body.locator('ul ul li')).to_have_text('嵌套项')
                        expect(body.locator('ol')).to_have_attribute('start', '3')
                        expect(body.locator('blockquote strong')).to_have_text('粗体')
                        expect(body.locator('em')).to_have_text('强调')
                        expect(body.locator('s')).to_have_text('删除')
                        expect(body.locator('p > code').first).to_have_text('inline **literal**')
                        expect(body.locator('.code-heading span')).to_have_text(['python', 'shell', 'text'])
                        expect(body.locator('.code-block pre').first).to_contain_text('<script>literal</script>')
                        body.locator('[data-copy-code]').nth(1).click()
                        assert page.evaluate('window.copiedCode') == "echo 'second code block'\n"
                        link = body.locator('a').filter(has_text='Relay 文档')
                        expect(link).to_have_attribute('href', 'https://relay.example.test/docs?a=1&b=2')
                        expect(link).to_have_attribute('rel', 'noopener noreferrer')
                        assert body.locator('script,img,iframe,[onerror]').count() == 0
                        assert all(body.locator('svg').evaluate_all('(nodes)=>nodes.map(el=>!!el.closest(".katex"))'))
                        assert_math(page,body)
                        assert not page.evaluate('window.markdownInjected || false')
                        assert all(not href.lower().startswith(('javascript:', 'data:', 'vbscript:')) for href in body.locator('a').evaluate_all('(nodes)=>nodes.map(el=>el.getAttribute("href"))'))
                        user = page.locator('#messages .user .message-body').first
                        expect(user).to_have_text('# User **literal** <b>tag</b>')
                        assert user.locator('h1,strong,b').count() == 0
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        if name == 'WebKit':
                            wrap = body.locator('.markdown-table-wrap')
                            assert wrap.evaluate('(el)=>el.scrollWidth>el.clientWidth')
                            wrap.evaluate('(el)=>{el.scrollLeft=150;}')
                            assert wrap.evaluate('(el)=>el.scrollLeft') > 0

                    # Incomplete Markdown remains readable during a running turn.
                    agent = agents['codex']
                    page.locator('[data-agent="' + agent['id'] + '"]').evaluate('(el)=>el.click()')
                    job = client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'stream fixture'})
                    device.request('POST', '/api/device/poll', {'capacity': 1})
                    partial = '## 正在更新\n\n\\[\\zeta(s)=\\frac1{2^s}'
                    device.request('POST', '/api/device/poll', {'active': [{'id': job['id'], 'output': partial}]})
                    live = page.locator('#messages .assistant .message-body').last
                    expect(live.locator('h2')).to_have_text('正在更新', timeout=15000)
                    expect(live.locator('.katex')).to_have_count(0)
                    expect(live).to_contain_text(r'\zeta(s)')
                    expect(page.locator('#stop-generation')).to_be_visible()
                    final = partial + '\\]\n\n| 完成 | 结果 |\n| --- | --- |\n| 是 | 正常 |'
                    device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'output': final})
                    expect(live.locator('table')).to_have_count(1, timeout=15000)
                    expect(live.locator('.katex')).to_have_count(1)
                    # Drain background polling before navigating; WebKit reports
                    # requests cancelled by reload as access-control console errors.
                    page.evaluate("Object.defineProperty(document, 'hidden', {get: () => true})")
                    page.wait_for_load_state('networkidle')
                    page.reload()
                    expect(page.locator('#messages .assistant .message-body').last.locator('table td')).to_have_text(['是', '正常'])
                    expect(page.locator('#messages .assistant .message-body').last.locator('.katex')).to_have_count(1)
                    assert not errors, errors
                    assert not external, external
                    browser.close()
            print('Markdown/math passed: Codex/Claude, bracket/dollar formulas, nested/list/matrix/long formulas, strict CSP/fonts, mobile scroll, streaming/reload, code/currency preservation and injection checks in Chromium/WebKit.')
        finally:
            server.close()


if __name__ == '__main__':
    main()
