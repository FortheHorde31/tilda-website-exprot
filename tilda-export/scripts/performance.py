"""Conservative HTML performance hints. No network, CSS execution or Lighthouse claims."""
from collections import Counter
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, {key: value or '' for key, value in attrs}))


def audit(documents, config=None):
    """documents: iterable of (filename, HTML). Findings are advisory, never a score."""
    findings, refs, preloads, images, origins = [], [], [], [], []
    lcp = (config or {}).get('lcp', {})
    lcp_src = lcp.get('src') if isinstance(lcp, dict) else None
    high_images, font_imports, font_faces = 0, 0, 0

    def add(code, file, detail):
        findings.append({'code': code, 'file': file, 'severity': 'review', 'detail': detail})

    for file, document in documents:
        tags = Tags()
        tags.feed(document)
        for tag, attrs in tags.items:
            rel = attrs.get('rel', '').lower().split()
            if tag == 'img':
                src = attrs.get('src', '')
                images.append(src)
                is_lcp = attrs.get('data-performance-role') == 'lcp' or bool(lcp_src and src == lcp_src)
                if attrs.get('fetchpriority') == 'high':
                    high_images += 1
                if is_lcp and attrs.get('loading') == 'lazy':
                    add('lcp-lazy', file, 'Отмеченное LCP-изображение использует lazy loading.')
                if is_lcp and attrs.get('fetchpriority') != 'high':
                    add('lcp-priority', file, 'У отмеченного LCP-изображения не задан fetchpriority=high; проверь необходимость.')
                if not (attrs.get('width') and attrs.get('height')) and 'aspect-ratio' not in attrs.get('style', ''):
                    add('image-space', file, 'Для изображения не найдены width/height в HTML; проверь резерв места в CSS.')
                if src.startswith('data:') and len(src) > 16384:
                    add('large-inline-image', file, 'Крупное встроенное изображение: сравни с отдельным кэшируемым ресурсом.')
            if tag == 'iframe' and not (attrs.get('width') and attrs.get('height')) and 'aspect-ratio' not in attrs.get('style', ''):
                add('iframe-space', file, 'Для iframe проверь резерв места в CSS и загрузку вне первого экрана.')
            if tag == 'link' and 'preconnect' in rel:
                url = attrs.get('href', '')
                parsed = urlsplit(url)
                origins.append(url.rstrip('/'))
                if parsed.scheme != 'https' or not parsed.netloc or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                    add('preconnect-origin', file, 'Preconnect должен указывать HTTPS origin без пути и параметров.')
            if tag == 'link' and 'preload' in rel:
                preloads.append((file, attrs))
                if attrs.get('as') == 'font' and (attrs.get('type') != 'font/woff2' or 'crossorigin' not in attrs):
                    add('font-preload', file, 'Для WOFF2 preload проверь as=font, type=font/woff2 и crossorigin.')
                if attrs.get('as') == 'image' and attrs.get('imagesrcset'):
                    add('responsive-preload', file, 'Сверь imagesrcset/imagesizes с реальным выбором изображения в браузере.')
            if tag == 'link' and 'stylesheet' in rel and attrs.get('href'):
                refs.append(('stylesheet', attrs['href'], file))
            if tag == 'script' and attrs.get('src'):
                refs.append(('script', attrs['src'], file))
                if attrs.get('type', '').lower() != 'module' and 'async' not in attrs and 'defer' not in attrs:
                    add('script-order', file, 'Внешний classic script без async/defer: проверь блокировку и зависимости до изменения.')
        # Analyze CSS only: do not match font-looking strings inside JS or prose.
        for style in re.findall(r'<style\b[^>]*>(.*?)</style\s*>', document, re.I | re.S):
            style = re.sub(r'/\*.*?\*/', '', style, flags=re.S)
            for match in re.finditer(r'@import\s+(?:url\(\s*)?[\"\x27]([^\"\x27]+)[\"\x27]', style, re.I):
                url = match.group(1)
                refs.append(('stylesheet', url, file))
                if 'font' in url.lower():
                    font_imports += 1
                    add('font-import', file, 'Шрифт подключается через CSS @import; рассмотри общее подключение в HEAD.')
                else:
                    add('css-import', file, 'CSS @import добавляет зависимость загрузки; проверь перенос в HEAD.')
            for face in re.findall(r'@font-face\s*\{([^}]*)\}', style, re.I | re.S):
                font_faces += 1
                if not re.search(r'font-display\s*:\s*swap\b', face, re.I):
                    add('font-display', file, 'В @font-face не найден swap; проверь стратегию показа и сдвиги fallback.')
    counts = Counter((kind, url) for kind, url, _ in refs)
    for (kind, url), count in counts.items():
        if count > 1:
            add('duplicate-resource', next(file for k, u, file in refs if (k, u) == (kind, url)),
                f'{kind} подключён {count} раз: {url}. Это дубли объявлений, не доказанные повторные сетевые запросы.')
    for origin, count in Counter(origins).items():
        if count > 1:
            add('duplicate-preconnect', 'HEAD', f'Повтор preconnect {origin}: {count}')
    if len(set(origins)) > 4:
        add('many-preconnects', 'HEAD', 'Больше четырёх origins: проверь, какие нужны первому экрану.')
    font_preloads = [(file, item) for file, item in preloads if item.get('as') == 'font']
    if len(font_preloads) > 2:
        add('many-font-preloads', 'HEAD', 'Несколько font preload: проверь конкуренцию с LCP и нужные наборы/веса.')
    for file, item in preloads:
        if item.get('as') == 'image' and not item.get('imagesrcset') and item.get('href') not in images:
            add('image-preload-match', file, 'Image preload не совпал с img src. Проверь CSS-фон, srcset и реальный запрос до изменения.')
    for (url, kind), count in Counter((item.get('href'), item.get('as')) for _, item in preloads).items():
        if count > 1:
            add('duplicate-preload', 'HEAD', f'Preload повторяется: {kind} {url}')
    if high_images > 2:
        add('many-high-images', 'page', 'Несколько изображений с high priority: проверь конкуренцию за сеть.')
    if lcp_src and lcp_src not in images:
        add('lcp-not-found', 'manifest', 'Заданный LCP src не найден в img; проверь currentSrc, CSS-фон или устаревшую настройку.')
    return {
        'status': 'static-audit-only', 'findings': findings,
        'inventory': {'images': len(images), 'font_imports': font_imports,
                      'font_faces': font_faces, 'font_preloads': len(font_preloads),
                      'preconnect_origins': sorted(set(origins))},
        'manual_checks': ['Фактический LCP и загрузка ресурсов в браузере',
                          'Внешний вид, CLS, функции и интерактив',
                          'Сопоставимые замеры скорости до и после'],
        'measurement': 'not-performed',
    }
