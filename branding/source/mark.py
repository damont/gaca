"""Gaca mark geometry, built as filled, non-overlapping shapes (one path per colour) with skia path ops.

A wing is five curved feathers stacked like a bird's: the top one in the body colour, then four crew colours,
each shorter and curling up at the tip. Gaps between overlapping pieces are real cut-outs, so every export
(SVG, Android vector, PNG, monochrome) is just filled paths.
"""
import skia

SLATE, PALE = '#2D3C4C', '#F0F4F9'
FEATHERS = ['#3F8E93', '#B0923F', '#8A6A9E', '#4E7093']   # teal, gold, plum, blue: from the crew palette
ROWS = [(31, 15), (29, 12), (25, 9), (20, 6), (14, 3)]      # (length, curl) per feather, top to bottom
GAP, SW, OUTLINE = 6.4, 6.4, 1.3                            # row spacing, feather thickness, cut-out width each side


def stroke(path, width):
    p = skia.Paint(Style=skia.Paint.kStroke_Style, StrokeWidth=width, StrokeCap=skia.Paint.kRound_Cap,
                   StrokeJoin=skia.Paint.kRound_Join, AntiAlias=True)
    out = skia.Path()
    p.getFillPath(path, out)
    return out


def op(a, b, kind):
    return skia.Op(a, b, kind) or skia.Path()


class Canvas:
    """Painter's algorithm on paths: painting a shape erases it from everything beneath."""
    def __init__(self):
        self.layers = []                     # [colour key, path] in paint order

    def paint(self, path, colour=None):      # colour None = background, i.e. a cut-out
        for layer in self.layers:
            layer[1] = op(layer[1], path, skia.PathOp.kDifference_PathOp)
        if colour is not None:
            self.layers.append([colour, path])

    def merged(self):
        """One path per colour key, in first-painted order."""
        out = {}
        for key, path in self.layers:
            out[key] = op(out[key], path, skia.PathOp.kUnion_PathOp) if key in out else path
        return out


def feather_paths(i, length, curl):
    p = skia.Path()
    y = i * GAP
    p.moveTo(0, y)
    p.quadTo(-length * .6, y, -length, y - curl)
    return stroke(p, SW), stroke(p, SW + 2 * OUTLINE)


def wing(cv, x, y, s, rot, flip=False, outline=OUTLINE, droop=False):
    """droop=True curls the feather tips down instead of up, for a resting wing."""
    m = skia.Matrix()
    m.setTranslate(x, y)
    if flip:
        m.preScale(-1, 1)
    m.preRotate(rot)
    m.preScale(s, s)
    pieces = []
    for i, (L, k) in enumerate(ROWS):
        p = skia.Path(); yy = i * GAP
        p.moveTo(0, yy); p.quadTo(-L * .6, yy, -L, yy + k if droop else yy - k)
        body, cut = stroke(p, SW), stroke(p, SW + 2 * outline / s)
        body.transform(m); cut.transform(m)
        pieces.append((body, cut, 'body' if i == 0 else f'f{i-1}'))
    for body, cut, key in reversed(pieces):          # bottom feather first, so each upper one overlaps it
        cv.paint(cut)
        cv.paint(body, key)


def you(cv, cx, cy, r, ring=1.5):
    cv.paint(skia.Path.Circle(cx, cy, r + ring))
    cv.paint(skia.Path.Circle(cx, cy, r), 'body')


def line(cv, pts, width, key):
    p = skia.Path(); p.moveTo(*pts[0])
    for seg in pts[1:]:
        p.cubicTo(*seg) if len(seg) == 6 else p.lineTo(*seg)
    cv.paint(stroke(p, width), key)


def build(pose, simple=False):
    """simple=True drops the trailing string and widens gaps, for tiny sizes (status-bar icon)."""
    cv = Canvas()
    o = 2.4 if simple else OUTLINE
    if pose == 'flight':
        wing(cv, 54, 46, .9, 30, outline=o); you(cv, 60, 54, 10, ring=o + .2)
        if not simple:
            line(cv, [(54, 63), (50, 68)], 3.5, 'body')
            line(cv, [(46, 73), (43, 76, 39, 78, 34, 78)], 3.5, 'tail')
    elif pose == 'high':
        for f in (False, True): wing(cv, 59 if f else 49, 52, .78, 45, f, outline=o)
        you(cv, 54, 58, 9.5, ring=o + .2)
    elif pose == 'level':
        for f in (False, True): wing(cv, 60 if f else 48, 47, .8, 0, f, outline=o)
        you(cv, 54, 54, 10, ring=o + .2)
    elif pose == 'down':                             # wings folded down, at rest
        for f in (False, True): wing(cv, 65 if f else 43, 50, .8, -20, f, outline=o, droop=True)
        you(cv, 54, 50, 10, ring=o + .2)
    return cv.merged()


POSES = ['flight', 'high', 'level', 'down']
