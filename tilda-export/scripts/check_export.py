#!/usr/bin/env python3
"""Validate a Tilda export manifest and build a LOCAL approximation. No network I/O."""
import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from performance import audit


class Fragment(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.classes, self.ids, self.links, self.resources, self.document = [], [], [], [], []

    def handle_decl(self, decl):
        self.document.append(decl)

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or '' for key, value in attrs}
        self.classes.extend(attrs.get('class', '').split())
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag in ('html', 'head', 'body', 'base'):
            self.document.append(tag)
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        for attr in ('src', 'poster', 'action'):
            if attrs.get(attr):
                self.resources.append(attrs[attr])
        if tag == 'link' and attrs.get('href'):
            self.resources.append(attrs['href'])


def inspect(manifest_path):
    """Return (report, [(manifest block, source)]). Never execute supplied HTML."""
    manifest_path = Path(manifest_path).resolve()
    base = manifest_path.parent
    errors, warnings, sources, checks = [], [], [], []
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'ok': False, 'errors': [f'Манифест: {exc}'], 'warnings': [], 'blocks': []}, []
    if not isinstance(manifest, dict) or manifest.get('version') != 1:
        return {'ok': False, 'errors': ['Нужен объект манифеста version: 1'], 'warnings': [], 'blocks': []}, []
    blocks = manifest.get('blocks')
    if not isinstance(blocks, list) or not blocks:
        return {'ok': False, 'errors': ['blocks должен быть непустым массивом'], 'warnings': [], 'blocks': []}, []
    head, head_code = None, ''
    head_file = manifest.get('head_file')
    if head_file is not None:
        if not isinstance(head_file, str) or not head_file:
            errors.append('head_file должен быть непустым относительным путём')
        else:
            path = (base / head_file).resolve()
            if Path(head_file).is_absolute() or not path.is_relative_to(base):
                errors.append('head_file выходит из папки экспорта')
            else:
                try:
                    head_code = path.read_bytes().decode('utf-8')
                    fragment = Fragment()
                    fragment.feed(head_code)
                    if fragment.document:
                        errors.append('head_file должен быть фрагментом без doctype/html/head/body/base')
                    for resource in fragment.resources:
                        if not resource.startswith(('https://', 'data:')):
                            errors.append('head_file: ресурс требует публичного HTTPS URL')
                    head = {'file': head_file, 'sha256': hashlib.sha256(head_code.encode('utf-8')).hexdigest()}
                except (OSError, UnicodeError) as exc:
                    errors.append(f'Не прочитан head_file: {exc}')
    perf_config = manifest.get('performance', {})
    if not isinstance(perf_config, dict):
        errors.append('performance должен быть объектом')
        perf_config = {}
    elif 'lcp' in perf_config and not isinstance(perf_config['lcp'], dict):
        errors.append('performance.lcp должен быть объектом')
        perf_config = {}
    files, roots, hooks, all_ids, links = set(), set(), set(), set(), []
    for index, block in enumerate(blocks, 1):
        label = f'Блок {index}'
        if not isinstance(block, dict):
            errors.append(f'{label}: нужен объект')
            continue
        kind, file, root = block.get('type'), block.get('file'), block.get('root')
        if kind not in ('html', 'popup'):
            errors.append(f'{label}: type должен быть html или popup')
        if not isinstance(file, str) or not file:
            errors.append(f'{label}: нужен путь file')
            continue
        path = (base / file).resolve()
        if Path(file).is_absolute() or not path.is_relative_to(base):
            errors.append(f'{label}: путь выходит из папки экспорта: {file}')
            continue
        if path in files:
            errors.append(f'{label}: файл повторяется: {file}')
        files.add(path)
        if not isinstance(root, str) or not re.fullmatch(r'[a-zA-Z_][\w-]*', root):
            errors.append(f'{label}: root должен быть одним CSS-классом без точки')
            root = ''
        if root in roots:
            errors.append(f'{label}: корень повторяется: {root}')
        roots.add(root)
        if kind == 'popup':
            hook = block.get('hook')
            if not isinstance(hook, str) or not re.fullmatch(r'#popup:[\w-]+', hook):
                errors.append(f'{label}: нужен hook вида #popup:contact')
            elif hook in hooks:
                errors.append(f'{label}: повтор hook: {hook}')
            else:
                hooks.add(hook)
        try:
            code = path.read_bytes().decode('utf-8')
        except (OSError, UnicodeError) as exc:
            errors.append(f'{label}: не прочитан {file}: {exc}')
            continue
        fragment = Fragment()
        fragment.feed(code)
        if not code.strip():
            errors.append(f'{file}: пустой файл')
        if fragment.document:
            errors.append(f'{file}: нужен HTML-фрагмент без doctype/html/head/body/base')
        if fragment.classes.count(root) != 1:
            errors.append(f'{file}: нужен ровно один элемент с root-классом {root}')
        for element_id in fragment.ids:
            if element_id in all_ids:
                errors.append(f'{file}: повтор ID: {element_id}')
            all_ids.add(element_id)
        links.extend((file, link) for link in fragment.links)
        for resource in fragment.resources:
            if not resource.startswith(('https://', 'data:')):
                errors.append(f'{file}: ресурс требует публичного HTTPS URL: {resource}')
        if re.search(r'(?:file://|localhost:|127\.0\.0\.1:)', code, re.I):
            errors.append(f'{file}: найдена ссылка на локальный компьютер')
        if re.search(r'<script\b[^>]*type=[\"\x27]importmap', code, re.I):
            warnings.append(f'{file}: importmap требует проверки на целевой странице')
        if re.search(r'(?:srcset\s*=|url\s*\(|@import)', code, re.I):
            warnings.append(f'{file}: проверь вручную URL в CSS/srcset и загрузку ресурсов')
        sources.append((block, code))
        checks.append({'file': file, 'type': kind, 'root': root,
                       'sha256': hashlib.sha256(code.encode('utf-8')).hexdigest(),
                       'bytes': len(code.encode('utf-8'))})
    for file, link in links:
        if link.startswith('#popup:') and link not in hooks:
            errors.append(f'{file}: попап не объявлен: {link}')
        elif link.startswith('#') and link != '#' and not link.startswith('#popup:') and link[1:] not in all_ids:
            errors.append(f'{file}: якорь не найден: {link}')
        elif not link.startswith('#') and not urlsplit(link).scheme:
            warnings.append(f'{file}: проверь адрес ссылки на сайте Tilda: {link}')
    if not any(block.get('type') == 'html' for block in blocks if isinstance(block, dict)):
        errors.append('Нужен хотя бы один блок html')
    warnings.append('CSS/JS, внешний backend и визуальное совпадение проверяются отдельно в браузере.')
    performance = audit([(b['file'], code) for b, code in sources] + ([('HEAD', head_code)] if head else []), perf_config)
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'blocks': checks,
            'head': head, 'performance': performance,
            'visual_check': 'not-performed', 'tilda_check': 'not-performed'}, sources


def render_preview(sources, head_code=''):
    content = []
    for index, (block, code) in enumerate(sources, 1):
        if block['type'] == 'html':
            content.append(f'<div class="t-rec" data-local-record="{index}">{code}</div>')
        else:
            hook = html.escape(block['hook'], quote=True)
            content.append(f'<dialog data-local-popup="{hook}"><button type="button" '
                           f'data-local-close aria-label="Закрыть">Закрыть</button>{code}</dialog>')
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Локальное превью экспорта Tilda</title>''' + head_code + '''
<style>body{margin:0}dialog{border:0;padding:24px;max-width:calc(100vw - 64px)}
dialog::backdrop{background:#0008}[data-local-close]{display:block;margin:0 0 16px auto}</style>
</head><body><!-- Приблизительное локальное превью, не рендер Tilda. -->
<div id="allrecords" class="t-records">''' + '\n'.join(content) + '''</div>
<script>
document.addEventListener('click', event => {
  const close = event.target.closest('[data-local-close]');
  if (close) { close.closest('dialog').close(); return; }
  const link = event.target.closest('a[href^="#popup:"]');
  if (!link) return;
  const dialog = [...document.querySelectorAll('[data-local-popup]')]
    .find(item => item.dataset.localPopup === link.getAttribute('href'));
  if (dialog) { event.preventDefault(); dialog.showModal(); }
});
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    report, sources = inspect(args.manifest)
    base = args.manifest.resolve().parent
    # Never replace the user's arbitrary files under the generated output names.
    for name, marker in [('checks.json', '"tilda_export_report": true'),
                         ('preview.html', '<!-- generated:tilda-export -->')]:
        target = base / name
        if target.is_symlink() or (target.exists() and marker not in target.read_text(encoding='utf-8')):
            print(f'Не перезаписываю существующий файл: {target}', file=sys.stderr)
            return 1
    report['tilda_export_report'] = True
    (base / 'checks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if report['ok']:
        head_code = (base / report['head']['file']).read_bytes().decode('utf-8') if report.get('head') else ''
        if report.get('head') and hashlib.sha256(head_code.encode('utf-8')).hexdigest() != report['head']['sha256']:
            print('HEAD изменился после проверки; повтори запуск.', file=sys.stderr)
            return 1
        (base / 'preview.html').write_text('<!-- generated:tilda-export -->\n' + render_preview(sources, head_code), encoding='utf-8')
    else:
        # An earlier successful preview is stale; do not present it as current.
        print('Экспорт не прошёл проверку. Старое превью, если есть, не обновлено.', file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
