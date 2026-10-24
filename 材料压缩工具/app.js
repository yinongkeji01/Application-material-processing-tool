'use strict';
const $ = selector => document.querySelector(selector);
const token = $('meta[name="tool-token"]').content;
let pages = [], outputs = [], busy = false, needsRestart = false;
let outputUrls = [];

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'X-Tool-Token': token, ...(options.headers || {})}});
  if (!response.ok) {
    let message = '处理失败，请重试。';
    try { message = (await response.json()).error || message; } catch (_) {}
    throw new Error(message);
  }
  return response;
}
async function post(path, data) {
  return (await api(path, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)})).json();
}
function status(message, error = false) {
  $('#status').hidden = !message;
  $('#status').textContent = message;
  $('#status').classList.toggle('error', error);
}
function setBusy(value) {
  busy = value;
  document.body.classList.toggle('busy', value);
  document.querySelectorAll('button, select, input').forEach(el => { el.disabled = value || needsRestart; });
  $('#shutdown').disabled = value;
  if (!value) {
    $('#convert').disabled = needsRestart || !pages.some(page => page.selected);
    $('#clear').disabled = needsRestart || pages.length === 0;
  }
}
function invalidate() {
  outputs = [];
  outputUrls.forEach(url => URL.revokeObjectURL(url));
  outputUrls = [];
  $('#results-section').hidden = true;
  $('#save-status').hidden = true;
  $('#results').replaceChildren();
}
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function renderPages() {
  const selected = pages.filter(page => page.selected).length;
  const fileCount = new Set(pages.map(page => page.sourceId)).size;
  $('#page-count').textContent = pages.length ? `已导入 ${fileCount} 个文件 · ${pages.length} 页 · 已选 ${selected} 页` : '还没有导入材料';
  $('#select-all').hidden = !pages.length;
  $('#select-all').textContent = selected === pages.length ? '取消全选' : '全选';
  $('#convert').disabled = busy || needsRestart || !selected;
  $('#clear').disabled = busy || needsRestart || !pages.length;
  const list = $('#page-list'); list.replaceChildren();
  if (!pages.length) {
    const empty = node('div', 'empty-source');
    empty.append(node('span', '', '▤'), node('p', '', '学籍报告、毕业证、成绩单……\n可以一起导入，默认按原文件分别输出。'));
    list.append(empty); return;
  }
  pages.forEach((page, index) => {
    const card = node('article', `page-card${page.selected ? '' : ' unselected'}`);
    const checkbox = node('input', 'check'); checkbox.type = 'checkbox'; checkbox.checked = page.selected;
    checkbox.setAttribute('aria-label', `选择 ${page.name}`);
    checkbox.addEventListener('change', () => { page.selected = checkbox.checked; invalidate(); renderPages(); });
    const wrap = node('div', 'thumb-wrap');
    const image = node('img'); image.src = page.thumbnail; image.alt = page.name; image.style.transform = `rotate(${page.rotation}deg)`;
    wrap.append(image);
    const name = node('p', 'page-name', `${index + 1}. ${page.name}`); name.title = page.name;
    const controls = node('div', 'page-controls');
    const button = (text, title, action, disabled = false) => {
      const el = node('button', '', text); el.title = title; el.setAttribute('aria-label', title); el.disabled = busy || disabled;
      el.addEventListener('click', action); controls.append(el);
    };
    button('←', '向前移动', () => { [pages[index - 1], pages[index]] = [pages[index], pages[index - 1]]; invalidate(); renderPages(); }, index === 0);
    button('→', '向后移动', () => { [pages[index + 1], pages[index]] = [pages[index], pages[index + 1]]; invalidate(); renderPages(); }, index === pages.length - 1);
    button('↻', '顺时针旋转 90 度', () => { page.rotation = (page.rotation + 90) % 360; invalidate(); renderPages(); });
    button('×', '移除此页', async () => {
      setBusy(true);
      try { await post('/api/remove', {id:page.id}); pages = pages.filter(item => item.id !== page.id); invalidate(); }
      catch (error) { status(error.message, true); }
      finally { setBusy(false); renderPages(); }
    });
    card.append(checkbox, wrap, name, controls); list.append(card);
  });
}
async function importFiles(files) {
  if (busy || needsRestart || !files.length) return;
  setBusy(true); invalidate();
  let count = 0; const errors = [];
  for (const file of files) {
    status(`正在导入 ${file.name}，PDF 多页解析可能需要一些时间……`);
    try {
      if (file.size > 80 * 1024 * 1024) throw new Error('单文件需小于 80MB。');
      const response = await api('/api/import', {method:'POST', headers:{'X-File-Name':encodeURIComponent(file.name), 'Content-Type':'application/octet-stream'}, body:file});
      const data = await response.json();
      pages.push(...data.pages.map(page => ({...page, selected:true, rotation:0})));
      count += data.pages.length;
    } catch (error) { errors.push(`${file.name}：${error.message}`); }
  }
  setBusy(false); renderPages(); $('#files').value = '';
  status(errors.length ? `已导入 ${count} 页。${errors.join('；')}` : `成功导入 ${count} 页。选择输出方式后，点击“生成合规 JPG”。`, errors.length > 0);
}
$('#files').addEventListener('change', event => importFiles([...event.target.files]));
$('#dropzone').addEventListener('keydown', event => { if (!busy && ['Enter',' '].includes(event.key)) { event.preventDefault(); $('#files').click(); } });
['dragenter','dragover'].forEach(name => $('#dropzone').addEventListener(name, event => { event.preventDefault(); $('#dropzone').classList.add('dragging'); }));
['dragleave','drop'].forEach(name => $('#dropzone').addEventListener(name, event => { event.preventDefault(); $('#dropzone').classList.remove('dragging'); }));
$('#dropzone').addEventListener('drop', event => importFiles([...event.dataTransfer.files]));
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());
$('#select-all').addEventListener('click', () => { const selected = !pages.every(page => page.selected); pages.forEach(page => { page.selected = selected; }); invalidate(); renderPages(); });
$('#clear').addEventListener('click', async () => {
  setBusy(true);
  try { await post('/api/clear', {}); pages = []; invalidate(); status('已清空本次材料。'); }
  catch (error) { status(error.message, true); }
  finally { setBusy(false); renderPages(); }
});
document.querySelectorAll('[name="mode"], #layout, #color, #output-name').forEach(el => el.addEventListener('input', () => {
  invalidate();
  $('#merge-name-field').hidden = !['one', 'two'].includes($('[name="mode"]:checked').value);
}));
async function download(path, name) {
  try {
    const blob = await (await api(path)).blob();
    const url = URL.createObjectURL(blob); const anchor = node('a');
    anchor.href = url; anchor.download = name; document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) { status(error.message, true); }
}
$('#convert').addEventListener('click', async () => {
  setBusy(true); invalidate(); status('正在转换并逐张校验大小，请稍候……');
  $('#convert').textContent = '正在生成……';
  try {
    const data = await post('/api/convert', {
      pages: pages.filter(page => page.selected).map(({id, rotation}) => ({id, rotation})),
      mode: $('[name="mode"]:checked').value, layout: $('#layout').value,
      grayscale: $('#color').value === 'gray', name: $('#output-name').value
    });
    outputs = data.outputs;
    for (const output of outputs) {
      const response = await api(`/api/output/${output.id}`);
      const blob = await response.blob();
      if (blob.size >= 200000 || blob.size !== output.bytes) throw new Error('文件大小校验未通过，请重新生成。');
      const url = URL.createObjectURL(blob); outputUrls.push(url);
      const card = node('article', 'output-card');
      const preview = node('button', 'output-preview'); preview.title = '点击查看实际像素图片';
      const image = node('img'); image.src = url; image.alt = output.name; preview.append(image);
      preview.addEventListener('click', () => { $('#preview-image').src = url; $('#preview-title').textContent = output.name; $('#preview').showModal(); });
      const info = node('div', 'output-info');
      const meta = node('div', 'output-meta');
      meta.append(node('span', 'badge', `${(output.bytes / 1000).toFixed(1)} KB · 合规`), document.createTextNode(`JPG · ${output.width} × ${output.height} · ${output.pages} 页`));
      info.append(node('p', 'output-name', output.name), meta);
      if (output.warning) info.append(node('p', 'quality-note', data.mode === 'files'
        ? '该文件合并后页面较多或尺寸较小，请放大检查小字；不清楚时，可仅勾选此文件的页面，再选择“全部分成 2 张”。'
        : '合并后页面较多或尺寸较小，请放大检查小字；不清楚时可分成 2 张，或减少勾选页数。'));
      const save = node('button', 'secondary download-one', '下载这张 JPG ↓');
      save.addEventListener('click', () => download(`/api/output/${output.id}`, output.name));
      info.append(save); card.append(preview, info); $('#results').append(card);
    }
    $('#results-section').hidden = false;
    $('#result-summary').textContent = data.mode === 'files'
      ? `已处理 ${data.sourceCount} 个文件，生成 ${outputs.length} 张 JPG，保留各自原文件名，每张均小于 200KB。`
      : `已生成 ${outputs.length} 张 JPG，每张均小于 200KB。请检查内容完整、文字清晰。`;
    const sourceCounts = new Map();
    outputs.forEach(output => sourceCounts.set(output.sourceId, (sourceCounts.get(output.sourceId) || 0) + 1));
    $('#quantity-warning').hidden = data.mode !== 'pages' || ![...sourceCounts.values()].some(count => count > 2);
    $('#quantity-warning').textContent = '有原文件被拆成了超过 2 张图片。同一类材料最多上传 2 张时，可改为“按原文件批量输出”，或选择需要的页面合并。不同类材料可分别上传。';
    status(`转换完成！${outputs.length} 张图片均已通过大小检查。`);
    $('#results-section').scrollIntoView({behavior:'instant',block:'start'});
  } catch (error) { invalidate(); status(error.message, true); }
  finally { $('#convert').replaceChildren(document.createTextNode('生成合规 JPG '), node('span', '', '→')); setBusy(false); renderPages(); }
});
$('#download-all').addEventListener('click', () => download('/api/zip', '报名材料_JPG.zip'));
$('#save-local').addEventListener('click', async () => {
  setBusy(true);
  try {
    const data = await post('/api/save', {});
    $('#save-path').textContent = `已保存 ${data.count} 张 JPG：${data.path}`;
    $('#save-status').hidden = false;
    status(`JPG 已保存到本机文件夹，可点击“打开文件夹”查看。`);
  } catch (error) { status(error.message, true); }
  finally { setBusy(false); renderPages(); }
});
$('#open-folder').addEventListener('click', async () => {
  try { await post('/api/open-folder', {}); }
  catch (error) { status(error.message, true); }
});
$('#close-preview').addEventListener('click', () => $('#preview').close());
$('#shutdown').addEventListener('click', async () => {
  if (busy) return;
  try { await post('/api/shutdown', {}); pages = []; invalidate(); renderPages(); status('工具已退出，本次材料已从工具内存中清除。需要继续时，请重新双击启动文件。'); setBusy(true); }
  catch (error) { status(error.message, true); }
});
setBusy(true);
api('/api/state').then(response => response.json()).then(data => {
  if (data.version !== 2) {
    needsRestart = true;
    throw new Error('工具已更新。请点击页面底部“退出工具”，重新双击“启动材料压缩工具.bat”，再导入原文件。新版默认按原文件名一对一输出。');
  }
  pages = data.pages.map(page => ({...page, selected:true, rotation:0}));
  if (pages.length) status('已恢复此前导入的页面。刷新后请重新确认页面顺序、旋转和输出设置。');
}).catch(error => status(error.message, true)).finally(() => { setBusy(false); renderPages(); });
