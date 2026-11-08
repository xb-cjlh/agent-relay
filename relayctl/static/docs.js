'use strict';
const origin = location.origin;
const phrase = `请阅读 ${origin}/install-agent.md，帮我安装并连接 Relay CLI 和 agent-bridge skill，设置后台自启并同步整机会话，令牌优先复用本机已有配置，没有则提示我在终端隐藏输入。`;
document.getElementById('agent-instruction').textContent = phrase;
async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const previous = button.textContent; button.textContent = '已复制';
    setTimeout(() => { button.textContent = previous; }, 2000);
  } catch (_) { document.getElementById('copy-status').textContent = '请选中文本手动复制。'; }
}
document.getElementById('copy-agent').addEventListener('click', e => copy(phrase, e.currentTarget));

// The manual uses a small Markdown subset. Build DOM nodes so no Markdown can inject HTML.
function inline(node, text) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|https?:\/\/[^\s，。)]+|\[[^\]]+\]\([^\s)]+\))/g);
  for (const part of parts) {
    let child;
    if (part.startsWith('`') && part.endsWith('`')) { child = document.createElement('code'); child.textContent = part.slice(1,-1); }
    else if (part.startsWith('**') && part.endsWith('**')) { child = document.createElement('strong'); child.textContent = part.slice(2,-2); }
    else if (/^https?:\/\//.test(part)) { child = document.createElement('a'); child.href = part; child.textContent = part; }
    else if (/^\[.+\]\(.+\)$/.test(part)) {
      const match = part.match(/^\[(.+)\]\((.+)\)$/);
      child = document.createElement('a'); child.href = match[2]; child.textContent = match[1];
    } else { child = document.createTextNode(part); }
    node.append(child);
  }
}
function renderManual(text) {
  const lines = text.replaceAll('https://relay.chaojibot.com', origin).split('\n');
  const manual = document.getElementById('manual'), contents = document.getElementById('contents');
  manual.replaceChildren();
  let index = 0;
  const blockStart = line => !line || /^(#{1,3} |```|\| |\- |> )/.test(line);
  while (index < lines.length) {
    const line = lines[index++];
    if (!line.trim()) continue;
    if (line.startsWith('```')) {
      const code = [];
      while (index < lines.length && !lines[index].startsWith('```')) code.push(lines[index++]);
      index++;
      const pre = document.createElement('pre'), body = document.createElement('code');
      body.textContent = code.join('\n'); pre.append(body);
      const button = document.createElement('button'); button.className = 'copy-code'; button.textContent = '复制命令';
      button.addEventListener('click', () => copy(body.textContent, button));
      manual.append(pre,button); continue;
    }
    const heading = line.match(/^(#{1,3}) (.+)$/);
    if (heading) {
      const node = document.createElement('h' + heading[1].length); inline(node,heading[2]);
      if (heading[1].length === 2) {
        node.id = 'step-' + contents.children.length;
        const link = document.createElement('a'); link.href = '#' + node.id; link.textContent = heading[2]; contents.append(link);
      }
      manual.append(node); continue;
    }
    if (line.startsWith('| ')) {
      const wrap = document.createElement('div'), table = document.createElement('table'); wrap.className = 'table-wrap';
      const rows = [line]; while (index < lines.length && lines[index].startsWith('| ')) rows.push(lines[index++]);
      rows.filter(row => !/^\|[\s:|-]+\|$/.test(row)).forEach((row,i) => {
        const tr = document.createElement('tr');
        row.split('|').slice(1,-1).forEach(cell => { const td = document.createElement(i === 0 ? 'th' : 'td'); inline(td,cell.trim()); tr.append(td); });
        table.append(tr);
      });
      wrap.append(table); manual.append(wrap); continue;
    }
    if (line.startsWith('- ')) {
      const list = document.createElement('ul'); let item = line;
      do { const li = document.createElement('li'); inline(li,item.slice(2)); list.append(li); item = lines[index]; if (!item?.startsWith('- ')) break; index++; } while (true);
      manual.append(list); continue;
    }
    if (line.startsWith('> ')) { const quote = document.createElement('blockquote'); inline(quote,line.slice(2)); manual.append(quote); continue; }
    const parts = [line]; while (index < lines.length && !blockStart(lines[index])) parts.push(lines[index++]);
    const paragraph = document.createElement('p'); inline(paragraph,parts.join(' ')); manual.append(paragraph);
  }
}
fetch('/docs/install.md').then(response => {
  if (!response.ok) throw new Error(); return response.text();
}).then(renderManual).catch(() => { document.getElementById('manual').textContent = '文档暂时无法加载，请稍后刷新，或访问 /docs/install.md。'; });
fetch('/downloads/current.json').then(response => {
  if (!response.ok) throw new Error(); return response.json();
}).then(manifest => {
  document.getElementById('download-status').textContent = '当前版本：' + manifest.version;
  const list = document.getElementById('download-list');
  for (const asset of manifest.assets) {
    const node = document.createElement('li'), link = document.createElement('a');
    link.href = asset.url; link.textContent = `${asset.platform === 'darwin' ? 'macOS' : 'Linux'} · ${asset.arch === 'arm64' ? 'ARM64 / Apple Silicon' : 'x64 / Intel'}`;
    node.append(link); list.append(node);
  }
  const item = document.createElement('li'), link = document.createElement('a'); link.href = manifest.checksums; link.textContent = 'SHA-256 校验清单'; item.append(link); list.append(item);
}).catch(() => { document.getElementById('download-status').textContent = '此服务端尚未发布 CLI 安装包。'; });
