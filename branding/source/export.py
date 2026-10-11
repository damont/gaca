"""Exports the Gaca mark kit: SVG, PNG and Android vector drawables. Run: python3 export.py <out dir>"""
import math, os, re, sys, skia
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mark import build, POSES, SLATE, PALE, FEATHERS

OUT = sys.argv[1]
NAMES = {'flight': 'in-flight', 'high': 'wings-high', 'level': 'wings-level', 'down': 'wings-down'}
TAIL = {'light': '#9AA6B2', 'dark': '#8796A6'}


def colour(key, on):
    if key == 'body': return SLATE if on == 'light' else PALE
    if key == 'tail': return TAIL[on]
    return FEATHERS[int(key[1:])]


def points(path):
    n = path.countPoints()
    return [path.getPoint(i) for i in range(n)]


def fit(layers, size, radius=None, box=None):
    """Centre the mark in a size x size canvas, scaled so it stays inside `radius` of the centre
    (adaptive-icon safe zone) or inside a `box`-wide square."""
    union = skia.Path()
    for p in layers.values(): union.addPath(p)
    b = union.computeTightBounds()
    cx, cy = (b.left() + b.right()) / 2, (b.top() + b.bottom()) / 2
    if radius:
        r = max(math.hypot(p.x() - cx, p.y() - cy) for p in points(union))
        s = radius / r
    else:
        s = box / max(b.width(), b.height())
    m = skia.Matrix(); m.setTranslate(size / 2, size / 2); m.preScale(s, s); m.preTranslate(-cx, -cy)
    out = {}
    for k, p in layers.items():
        q = skia.Path(p); q.transform(m); out[k] = q
    return out


def crop(layers, pad=.04):
    """Translate so the mark sits at the origin with padding; returns (layers, width, height)."""
    union = skia.Path()
    for p in layers.values(): union.addPath(p)
    b = union.computeTightBounds()
    p_ = max(b.width(), b.height()) * pad
    m = skia.Matrix(); m.setTranslate(p_ - b.left(), p_ - b.top())
    out = {}
    for k, p in layers.items():
        q = skia.Path(p); q.transform(m); out[k] = q
    return out, b.width() + 2 * p_, b.height() + 2 * p_


def d(path):
    """Serialise a skia path to SVG path data (conics become quads), 2 decimal places."""
    f = lambda pt: f'{pt.x():.2f},{pt.y():.2f}'.replace('.00', '')
    out, it = [], skia.Path.Iter(path, False)
    while True:
        verb, pts = it.next()
        if verb == skia.Path.kDone_Verb: break
        if verb == skia.Path.kMove_Verb: out.append('M' + f(pts[0]))
        elif verb == skia.Path.kLine_Verb: out.append('L' + f(pts[1]))
        elif verb == skia.Path.kQuad_Verb: out.append('Q' + f(pts[1]) + ' ' + f(pts[2]))
        elif verb == skia.Path.kCubic_Verb: out.append('C' + ' '.join(f(p) for p in pts[1:]))
        elif verb == skia.Path.kConic_Verb:
            q = skia.Path.ConvertConicToQuads(pts[0], pts[1], pts[2], it.conicWeight(), 2)
            out += ['Q' + f(q[i]) + ' ' + f(q[i + 1]) for i in range(1, len(q) - 1, 2)]
        elif verb == skia.Path.kClose_Verb: out.append('Z')
    return ''.join(out)


def union(layers):
    u = skia.Path()
    for p in layers.values():
        u = skia.Op(u, p, skia.PathOp.kUnion_PathOp)
    return u


def write(rel, text):
    path = os.path.join(OUT, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').write(text)


def svg(layers, w, h, on=None, bg=None, mono=None):
    body = f'<rect width="{w:g}" height="{h:g}" fill="{bg}"/>' if bg else ''
    if mono:
        body += f'<path fill="{mono}" d="{d(union(layers))}"/>'
    else:
        body += ''.join(f'<path fill="{colour(k, on)}" d="{d(p)}"/>' for k, p in layers.items())
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.2f} {h:.2f}">{body}</svg>\n'


def vector(layers, size, on=None, mono=None, dp=None, tint=None):
    dp = dp or size
    paths = [(mono, union(layers))] if mono else [(colour(k, on), p) for k, p in layers.items()]
    body = '\n'.join(f'    <path\n        android:fillColor="{c}"\n        android:pathData="{d(p)}" />' for c, p in paths)
    tint_attr = f'\n    android:tint="{tint}"' if tint else ''
    return (f'<vector xmlns:android="http://schemas.android.com/apk/res/android"\n    android:width="{dp:g}dp"\n    android:height="{dp:g}dp"\n'
            f'    android:viewportWidth="{size:g}"\n    android:viewportHeight="{size:g}"{tint_attr}>\n{body}\n</vector>\n')


def png(layers, size, rel, on=None, bg=None, mono=None, w=None, h=None):
    w, h = w or size, h or size
    scale = size / max(w, h)
    surf = skia.Surface(round(w * scale), round(h * scale))
    with surf as c:
        c.clear(skia.Color4f.FromColor(int('FF' + bg[1:], 16)) if bg else skia.Color4f(0, 0, 0, 0))
        c.scale(scale, scale)
        items = [(mono, union(layers))] if mono else [(colour(k, on), p) for k, p in layers.items()]
        for col, p in items:
            c.drawPath(p, skia.Paint(Color=skia.Color4f.FromColor(int('FF' + col[1:], 16)), AntiAlias=True))
    path = os.path.join(OUT, rel); os.makedirs(os.path.dirname(path), exist_ok=True)
    surf.makeImageSnapshot().save(path)


# 1. The four marks, tightly cropped on transparent backgrounds: for light surfaces, dark surfaces, and one-colour.
for pose in POSES:
    layers, w, h = crop(build(pose))
    n = NAMES[pose]
    write(f'svg/gaca-{n}-on-light.svg', svg(layers, w, h, on='light'))
    write(f'svg/gaca-{n}-on-dark.svg', svg(layers, w, h, on='dark'))
    write(f'svg/gaca-{n}-mono.svg', svg(layers, w, h, mono='#000000'))
    for on in ('light', 'dark'):
        png(layers, 512, f'png/marks/gaca-{n}-on-{on}-512.png', on=on, w=w, h=h)
    png(layers, 512, f'png/marks/gaca-{n}-mono-512.png', mono='#2D3C4C', w=w, h=h)

# 2. App icon: In flight. Square, full-bleed background, mark inside the 66/108 adaptive safe zone.
icon = fit(build('flight'), 108, radius=30)
write('svg/app-icon-light.svg', svg(icon, 108, 108, on='light', bg=PALE))
write('svg/app-icon-dark.svg', svg(icon, 108, 108, on='dark', bg=SLATE))
# iOS shows the whole square (Android launchers crop to the middle 72/108), so the iOS mark is drawn larger.
ios = fit(build('flight'), 108, radius=38)
write('svg/app-icon-ios-light.svg', svg(ios, 108, 108, on='light', bg=PALE))
png(ios, 1024, 'png/app-icon/ios-app-icon-light-1024.png', on='light', bg=PALE, w=108, h=108)
png(ios, 1024, 'png/app-icon/ios-app-icon-dark-1024.png', on='dark', bg=SLATE, w=108, h=108)
png(ios, 1024, 'png/app-icon/ios-app-icon-tinted-1024.png', mono='#FFFFFF', bg='#000000', w=108, h=108)
png(ios, 512, 'png/app-icon/play-store-icon-512.png', on='light', bg=PALE, w=108, h=108)

# 3. Android resources, laid out as res/ folders so they can be copied straight in.
write('android/res/drawable/ic_launcher_foreground.xml', vector(icon, 108, on='light'))
write('android/res/drawable/ic_launcher_monochrome.xml', vector(icon, 108, mono='#FF000000'))
write('android/res/values/ic_launcher_colors.xml',
      f'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <color name="ic_launcher_background">{PALE}</color>\n</resources>\n')
adaptive = ('<?xml version="1.0" encoding="utf-8"?>\n<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
            '    <background android:drawable="@color/ic_launcher_background" />\n'
            '    <foreground android:drawable="@drawable/ic_launcher_foreground" />\n'
            '    <monochrome android:drawable="@drawable/ic_launcher_monochrome" />\n</adaptive-icon>\n')
write('android/res/mipmap-anydpi-v26/ic_launcher.xml', adaptive)
write('android/res/mipmap-anydpi-v26/ic_launcher_round.xml', adaptive)
# Status-bar icon: simplified In flight (no string, wider gaps) as a flat white silhouette.
note = fit(build('flight', simple=True), 24, box=20)
write('android/res/drawable/ic_notification.xml', vector(note, 24, mono='#FFFFFFFF'))
png(note, 96, 'png/app-icon/notification-silhouette-96.png', mono='#2D3C4C', w=24, h=24)
# In-app marks: day versions in drawable/, night versions (pale body) in drawable-night/.
for pose in POSES:
    layers = fit(build(pose), 108, box=100)
    name = 'ic_gaca_' + NAMES[pose].replace('-', '_')
    write(f'android/res/drawable/{name}.xml', vector(layers, 108, on='light', dp=48))
    write(f'android/res/drawable-night/{name}.xml', vector(layers, 108, on='dark', dp=48))
print('done')
