#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# WithoutBG — GIMP 3 Plugin
# Removes image backgrounds using a local WithoutBG server (Docker or Mac app),
# or, opt-in, withoutBG Pro (hosted API, needs an API key).
#
# Install:  place this directory inside GIMP's plug-ins folder, e.g.
#   ~/Library/Application Support/GIMP/3.0/plug-ins/   (macOS)
#   ~/.config/GIMP/3.0/plug-ins/                       (Linux)
# so the layout is:
#   plug-ins/withoutbg/withoutbg.py
#
# The server must be running at SERVER_URL before you invoke the plug-in.
#
# Menu:  Tools ▸ WithoutBG ▸ Remove Background…
#        Tools ▸ WithoutBG ▸ Smart Brush…
#        Tools ▸ WithoutBG ▸ Smart Brush: Add Paint Layers
#        Tools ▸ WithoutBG ▸ Smart Brush: Apply
#
# License: GNU General Public License v3 or later
# <https://www.gnu.org/licenses/>

import gi
gi.require_version('Gimp',   '3.0')
gi.require_version('GimpUi', '3.0')
gi.require_version('Gtk',    '3.0')
gi.require_version('Gegl',   '0.4')
gi.require_version('Gdk',    '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gimp, GimpUi, Gtk, Gdk, GdkPixbuf, GObject, GLib, Gio, Gegl

import io
import os
import sys
import json
import math
import base64
import threading
import traceback
import tempfile
import uuid
import urllib.request
import urllib.error

# ── Configuration ────────────────────────────────────────────────────────────

PLUGIN_VERSION = "1.3.0"

SERVER_URL = "http://127.0.0.1:8000"

# withoutBG Pro (opt-in). Key from the dialog, or the WITHOUTBG_API_KEY env var.
PRO_API_URL = os.environ.get("WITHOUTBG_API_URL", "https://api.withoutbg.com").rstrip("/")
PLUGIN_DOCS_URL = "https://withoutbg.com/open-model/plugins/gimp"
SMART_BRUSH_URL = "https://withoutbg.com/smart-brush"
PRO_KEY_URL = "https://withoutbg.com/pro-model-api?utm_source=gimp&utm_medium=plugin&utm_campaign=open-weights-v4&utm_content=withoutbg-gimp-dialog"

# The server downsamples input so the longest side is at most this many pixels.
# We match that locally before POSTing to avoid 413 payloads and to keep the
# returned matte aligned with what the model actually sees.
MAX_SERVER_SIDE = 1024

# Smart brush (open weights, local server only).
KEEP_LAYER_NAME   = 'withoutBG Keep'
REMOVE_LAYER_NAME = 'withoutBG Remove'
PAINT_PARASITE    = 'withoutbg-paint'          # on paint layers: b"keep:<tattoo>" / b"remove:<tattoo>"
CONTEXT_PARASITE  = 'withoutbg-edit-context'   # on the target layer: server editContext token
ENGINE_PARASITE   = 'withoutbg-engine'         # on the target layer: b'pro' or b'open' (engine of the cutout)
SERVER_PARASITE   = 'withoutbg-server-url'     # global: last local server used
STROKES_PARASITE  = 'withoutbg-pro-strokes'    # on the target layer: JSON of Pro dialog strokes

# Smart brush dialog.
BRUSH_KEEP_RGB    = (0.16, 0.78, 0.30)   # green: what went missing
BRUSH_REMOVE_RGB  = (0.93, 0.20, 0.20)   # red: what should go
BRUSH_ALPHA       = 0.55                 # stroke overlay opacity in the preview
BRUSH_SMOOTHING   = 0.35                 # 0..1, lower = smoother, lazier strokes
BRUSH_DEFAULT_PX  = 28                   # brush diameter in screen pixels
BG_OPACITY_PCT    = 30                   # how much of the removed background shows

# ── Internals ─────────────────────────────────────────────────────────────────

def _fit_longest_side(width, height, max_side=MAX_SERVER_SIDE):
    """Return (w, h) with longest side ≤ *max_side*, preserving aspect ratio."""
    scale = min(max_side / width, max_side / height, 1.0)
    if scale >= 1.0:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))

def _user_agent():
    """e.g. 'withoutbg-gimp/1.1.0 (GIMP 3.0.2; linux)'. No user identifiers."""
    try:
        gimp_ver = Gimp.version()
    except Exception:
        gimp_ver = "3"
    return f"withoutbg-gimp/{PLUGIN_VERSION} (GIMP {gimp_ver}; {sys.platform})"


def _request(url, data=None, method='GET'):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('User-Agent', _user_agent())
    return req


def _health_check(server_url):
    """Return (ok: bool, info: dict | str)."""
    try:
        with urllib.request.urlopen(_request(f"{server_url}/health"), timeout=5) as r:
            return True, json.loads(r.read())
    except Exception as exc:
        return False, str(exc)


def _export_png_image(image, path):
    """
    Write *image* as PNG to *path*.

    Uses 'file-png-export' (GIMP 3 name). The procedure operates on the
    image directly — no drawables argument required.
    """
    gfile = Gio.File.new_for_path(path)
    pdb   = Gimp.get_pdb()

    proc = pdb.lookup_procedure('file-png-export')
    if proc is None:
        raise RuntimeError("'file-png-export' procedure not found in this GIMP build.")
    cfg = proc.create_config()
    cfg.set_property('run-mode',        Gimp.RunMode.NONINTERACTIVE)
    cfg.set_property('image',           image)
    cfg.set_property('file',            gfile)
    cfg.set_property('options',         None)
    cfg.set_property('interlaced',      False)
    cfg.set_property('compression',     7)
    cfg.set_property('bkgd',            True)
    cfg.set_property('offs',            False)
    cfg.set_property('phys',            True)
    cfg.set_property('time',            False)
    cfg.set_property('save-transparent', True)
    result = proc.run(cfg)
    if result.index(0) != Gimp.PDBStatusType.SUCCESS:
        raise RuntimeError("'file-png-export' returned non-SUCCESS.")


def _export_layer_png(image, layer, path, max_side=None):
    """
    Export *layer*'s full drawable bounds as PNG to *path*.

    The image canvas may clip a moved layer, so build a temporary layer-sized
    image instead of flattening the current canvas.
    """
    width, height = layer.get_width(), layer.get_height()
    if width <= 0 or height <= 0:
        raise RuntimeError('Target layer has no exportable pixels.')

    export_img = Gimp.Image.new(width, height, image.get_base_type())
    try:
        export_layer = Gimp.Layer.new_from_drawable(layer, export_img)
        if export_layer is None:
            raise RuntimeError('Could not copy the target layer for export.')
        export_img.insert_layer(export_layer, None, 0)
        export_layer.set_offsets(0, 0)
        if export_layer.get_mask() is not None:
            # The server must see the layer's pixels, not the current cutout.
            export_layer.remove_mask(Gimp.MaskApplyMode.DISCARD)

        if max_side is not None:
            nw, nh = _fit_longest_side(width, height, max_side)
            if (nw, nh) != (width, height):
                export_img.scale(nw, nh)

        _export_png_image(export_img, path)
    finally:
        export_img.delete()


def _post_image(png_bytes, server_url, output_type):
    """POST *png_bytes* to the server. Returns (result_bytes, latency_str)."""
    url = f"{server_url}/v1/remove-background?output={output_type}"
    req = _request(url, data=png_bytes, method='POST')
    req.add_header('Content-Type', 'image/png')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body    = resp.read()
            latency = resp.headers.get('X-Latency-Ms', '?')
            return body, latency
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read()).get('error', str(exc))
        except Exception:
            msg = str(exc)
        raise RuntimeError(f"Server HTTP {exc.code}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach WithoutBG server at {server_url}\n{exc.reason}"
        ) from exc


_PRO_ERRORS = {
    401: 'withoutBG Pro rejected the API key. Check the key in the dialog.',
    402: 'Out of withoutBG Pro credits.',
    403: 'This API key cannot use withoutBG Pro.',
    429: 'withoutBG Pro rate limit reached. Try again shortly.',
}


def _post_pro(png_bytes, api_key, masks=None):
    """POST *png_bytes* to withoutBG Pro; returns (matte_png_bytes, latency_str)."""
    boundary = uuid.uuid4().hex
    # The photo as ``file``; Smart brush adds ``keep_mask`` / ``remove_mask`` PNGs.
    body = b''
    for name, data in {'file': png_bytes, **(masks or {})}.items():
        filename = 'image.png' if name == 'file' else f'{name}.png'
        body += (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            'Content-Type: image/png\r\n\r\n'
        ).encode() + data + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    req = _request(f'{PRO_API_URL}/v1.0/alpha-channel', data=body, method='POST')
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    req.add_header('X-API-Key', api_key)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read(), resp.headers.get('X-Latency-Ms', '?')
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            _PRO_ERRORS.get(exc.code, f'withoutBG Pro returned HTTP {exc.code}.')
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Cannot reach withoutBG Pro\n{exc.reason}') from exc


def _layer_offsets(layer):
    """Return *layer*'s image-space offsets."""
    ok, offset_x, offset_y = layer.get_offsets()
    if not ok:
        raise RuntimeError('Could not read target layer offsets.')
    return offset_x, offset_y


def _layer_bounds(layer):
    """Return *layer*'s image-space bounds as (x1, y1, x2, y2)."""
    offset_x, offset_y = _layer_offsets(layer)
    return (
        offset_x,
        offset_y,
        offset_x + layer.get_width(),
        offset_y + layer.get_height(),
    )


def _expand_canvas_to_layer(image, layer):
    """
    Expand *image* so *layer* is fully inside the canvas.

    Returns restore data for _restore_canvas(), or None if no expansion was
    needed.
    """
    image_w, image_h = image.get_width(), image.get_height()
    layer_x1, layer_y1, layer_x2, layer_y2 = _layer_bounds(layer)

    bounds_x1 = min(0, layer_x1)
    bounds_y1 = min(0, layer_y1)
    bounds_x2 = max(image_w, layer_x2)
    bounds_y2 = max(image_h, layer_y2)

    new_w = bounds_x2 - bounds_x1
    new_h = bounds_y2 - bounds_y1
    if new_w == image_w and new_h == image_h:
        return None

    # Preserve the original image coordinate system inside the expanded canvas.
    image.resize(new_w, new_h, -bounds_x1, -bounds_y1)
    return image_w, image_h, bounds_x1, bounds_y1


def _restore_canvas(image, restore_data):
    """Restore the canvas after _expand_canvas_to_layer()."""
    if restore_data is None:
        return

    image_w, image_h, bounds_x1, bounds_y1 = restore_data
    image.resize(image_w, image_h, bounds_x1, bounds_y1)


def _add_matte_mask(image, layer, matte_img):
    """
    Add the alpha matte to *layer* as an (unapplied) layer mask, in place.

    The server caps its output at 1024 px, so we only fetch the (small) alpha
    *matte*, scale it back up to the current layer bounds, and attach it as a
    layer mask. The RGB stays at native resolution — no quality loss from the
    server cap.

    The mask is left *unapplied* so the user can inspect, tweak, or discard the
    result. To commit it: Layer ▸ Mask ▸ Apply Layer Mask.
    """
    w, h = layer.get_width(), layer.get_height()

    # Scale the (≤1024 px) matte back up to layer-local coordinates.
    if matte_img.get_width() != w or matte_img.get_height() != h:
        matte_img.scale(w, h)
    matte_layer = matte_img.get_layers()[0]

    restore_data = _expand_canvas_to_layer(image, layer)
    try:
        # Reuse the layer's mask (Smart brush, re-runs), else attach a white one,
        # then copy the matte into it.
        mask = layer.get_mask()
        if mask is None:
            mask = layer.create_mask(Gimp.AddMaskType.WHITE)
            layer.add_mask(mask)

        Gimp.Selection.none(matte_img)
        if not Gimp.edit_copy([matte_layer]):
            raise RuntimeError('Could not copy matte pixels into the layer mask.')
        pasted = Gimp.edit_paste(mask, False)
        # GIMP 3 returns a list of pasted (floating) layers.
        floating = pasted[0] if isinstance(pasted, (list, tuple)) else pasted
        if floating is None:
            raise RuntimeError('Could not paste matte pixels into the layer mask.')

        # Floating selections use image coordinates; align to the layer's origin.
        offset_x, offset_y = _layer_offsets(layer)
        if not floating.set_offsets(offset_x, offset_y):
            raise RuntimeError('Could not align the matte with the target layer.')
        Gimp.floating_sel_anchor(floating)
    finally:
        _restore_canvas(image, restore_data)

    return mask


def _resolve_target(image, drawables):
    """Return the drawable to operate on, or None."""
    if drawables:
        return drawables[0]
    selected = image.get_selected_layers()
    if selected:
        return selected[0]
    layers = image.get_layers()
    return layers[0] if layers else None


def _server_status_markup(server_url):
    """Return a one-line Pango markup string describing server health."""
    ok, info = _health_check(server_url)
    if ok:
        model = info.get('model', '?') if isinstance(info, dict) else '?'
        ver   = info.get('version', '?') if isinstance(info, dict) else '?'
        return (
            f'Server ready at {server_url}  ·  model: {model}  ·  v{ver}'
        )
    return f'Server not reachable at {server_url} — {info}'


def _read_config(config):
    """Return (server_url, use_pro, api_key) from the procedure config."""
    def get(name, default):
        try:
            value = config.get_property(name)
        except Exception:
            return default
        return default if value in (None, '') else value

    api_key = (get('api-key', '') or os.environ.get('WITHOUTBG_API_KEY', '')).strip()
    return get('server-url', SERVER_URL), bool(get('use-pro', False)), api_key


def _link_label(links):
    """A label of clickable links, separated by '·'. *links*: [(text, url), ...]."""
    label = Gtk.Label(xalign=0)
    label.set_markup('   ·   '.join(
        f'<a href="{GLib.markup_escape_text(url)}">{GLib.markup_escape_text(text)}</a>'
        for text, url in links
    ))
    label.set_margin_top(6)
    label.set_margin_bottom(6)

    def open_link(_label, uri):
        # Python's webbrowser works the same on Linux, macOS and Windows builds.
        try:
            import webbrowser
            return bool(webbrowser.open(uri))
        except Exception:
            return False   # fall back to GTK's own handler

    label.connect('activate-link', open_link)
    return label


def _mask_api_key_entry(dialog):
    """Hide the API key (dots) with an eye icon to reveal it. Hidden by default.

    Creates the key widget before `dialog.fill` so fill reuses it. Any failure
    leaves the stock (visible) entry, so the dialog still works.
    """
    try:
        widget = dialog.get_widget('api-key', GObject.TYPE_NONE)
        entry = widget.get_entry() if hasattr(widget, 'get_entry') else widget
        if not isinstance(entry, Gtk.Entry):
            return
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        pos = Gtk.EntryIconPosition.SECONDARY
        entry.set_icon_from_icon_name(pos, 'gimp-visible')
        entry.set_icon_activatable(pos, True)
        entry.set_icon_tooltip_text(pos, 'Show / hide API key')

        def toggle(e, icon_pos, _event):
            if icon_pos == pos:
                e.set_visibility(not e.get_visibility())

        entry.connect('icon-press', toggle)
    except Exception:
        pass


# ── Smart brush helpers ───────────────────────────────────────────────────────

def _png_data_url(path):
    with open(path, 'rb') as fh:
        return 'data:image/png;base64,' + base64.b64encode(fh.read()).decode('ascii')


def _decode_data_url(value):
    if value.startswith('data:'):
        value = value.split(',', 1)[1]
    return base64.b64decode(value)


def _post_json(server_url, body):
    """POST a JSON Smart brush request to the local server; returns the JSON reply."""
    url = f"{server_url}/v1/remove-background"
    req = _request(url, data=json.dumps(body).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
            msg = err.get('detail') or err.get('reason') or err.get('error') or str(exc)
        except Exception:
            msg = str(exc)
        raise RuntimeError(f"Server HTTP {exc.code}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach WithoutBG server at {server_url}\n{exc.reason}"
        ) from exc


def _get_parasite_bytes(item, name):
    try:
        parasite = item.get_parasite(name) if item is not None else Gimp.get_parasite(name)
    except Exception:
        return None
    if parasite is None:
        return None
    return bytes(parasite.get_data())


def _set_parasite_bytes(item, name, data):
    parasite = Gimp.Parasite.new(name, Gimp.PARASITE_PERSISTENT, list(data))
    if item is None:
        Gimp.attach_parasite(parasite)
    else:
        item.attach_parasite(parasite)


def _clear_parasite(item, name):
    try:
        if item.get_parasite(name) is not None:
            item.detach_parasite(name)
    except Exception:
        pass


def _remember_server(server_url):
    try:
        _set_parasite_bytes(None, SERVER_PARASITE, server_url.encode())
    except Exception:
        pass


def _remembered_server():
    data = _get_parasite_bytes(None, SERVER_PARASITE)
    return data.decode('utf-8', 'replace') if data else SERVER_URL


def _all_layers(image):
    out, stack = [], list(image.get_layers())
    while stack:
        layer = stack.pop(0)
        out.append(layer)
        if layer.is_group():
            stack[0:0] = list(layer.get_children())
    return out


def _paint_role(layer):
    """('keep' | 'remove', target tattoo) for a Smart brush paint layer, else None."""
    data = _get_parasite_bytes(layer, PAINT_PARASITE)
    if not data:
        return None
    role, _, tattoo = data.decode('ascii', 'replace').partition(':')
    if role not in ('keep', 'remove') or not tattoo.isdigit():
        return None
    return role, int(tattoo)


def _smart_brush_target(image, drawables):
    """The layer Smart brush edits: the selected layer, the owner of a selected
    mask, or the layer a selected paint layer belongs to."""
    drawable = _resolve_target(image, drawables)
    if drawable is None:
        return None
    if isinstance(drawable, Gimp.LayerMask):
        return Gimp.Layer.from_mask(drawable)
    if not isinstance(drawable, Gimp.Layer):
        return None
    role = _paint_role(drawable)
    if role is not None:
        return image.get_layer_by_tattoo(role[1])
    return drawable


def _paint_layers(image, target):
    """{'keep': layer, 'remove': layer} painted for *target* (either may be missing)."""
    found = {}
    tattoo = target.get_tattoo()
    for layer in _all_layers(image):
        role = _paint_role(layer)
        if role is not None and role[1] == tattoo and role[0] not in found:
            found[role[0]] = layer
    return found


def _paint_layer_type(image):
    base = image.get_base_type()
    if base == Gimp.ImageBaseType.GRAY:
        return Gimp.ImageType.GRAYA_IMAGE
    if base == Gimp.ImageBaseType.INDEXED:
        return Gimp.ImageType.INDEXEDA_IMAGE
    return Gimp.ImageType.RGBA_IMAGE


def _is_painted(layer):
    """True when *layer* has any pixel at least half opaque."""
    if not layer.has_alpha():
        return True
    result = layer.histogram(Gimp.HistogramChannel.ALPHA, 0.5, 1.0)
    # (success, mean, std_dev, median, pixels, count, percentile)
    return bool(result[0]) and result[5] > 0


def _export_marks_png(image, layer, path, size):
    """Export *layer*'s paint as a white-on-transparent PNG of *size* (w, h).

    The server reads a pixel as marked when it is light and opaque after
    compositing onto black, so the paint colour does not matter.
    """
    w, h = layer.get_width(), layer.get_height()
    tmp = Gimp.Image.new(w, h, Gimp.ImageBaseType.RGB)
    try:
        copy = Gimp.Layer.new_from_drawable(layer, tmp)
        tmp.insert_layer(copy, None, 0)
        copy.set_offsets(0, 0)
        if not copy.has_alpha():
            copy.add_alpha()
        copy.set_opacity(100.0)
        copy.set_lock_alpha(True)
        Gimp.context_push()
        try:
            Gimp.context_set_foreground(Gegl.Color.new('white'))
            copy.edit_fill(Gimp.FillType.FOREGROUND)
        finally:
            Gimp.context_pop()
        if (w, h) != tuple(size):
            tmp.scale(*size)
        _export_png_image(tmp, path)
    finally:
        tmp.delete()


def _export_mask_png(layer, path, size):
    """Export *layer*'s layer mask as a grayscale PNG of *size* (w, h)."""
    mask = layer.get_mask()
    w, h = layer.get_width(), layer.get_height()
    tmp = Gimp.Image.new(w, h, Gimp.ImageBaseType.GRAY)
    try:
        copy = Gimp.Layer.new_from_drawable(mask, tmp)
        tmp.insert_layer(copy, None, 0)
        copy.set_offsets(0, 0)
        if (w, h) != tuple(size):
            tmp.scale(*size)
        _export_png_image(tmp, path)
    finally:
        tmp.delete()


# ── Smart brush dialog ────────────────────────────────────────────────────────
#
# The user paints Keep (green) / Remove (red) strokes straight on a preview of
# the cutout. Strokes are stored as points in preview pixels (the preview is
# the server size, ≤ 1024 px) and drawn as smooth Catmull-Rom splines, so a
# shaky hand still gives a clean, rough guide. The GTK part below only needs
# Gtk / Gdk / GdkPixbuf / cairo; the GIMP and server work is passed in.

RESPONSE_APPLY = 1


def _load_cairo():
    """Return the pycairo module wired into GTK, or None when unavailable."""
    try:
        gi.require_foreign('cairo')
        import cairo
        return cairo
    except Exception:
        return None


def _pixbuf_from_png(data, size=None):
    loader = GdkPixbuf.PixbufLoader.new_with_type('png')
    loader.write(data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if size is not None and (pixbuf.get_width(), pixbuf.get_height()) != tuple(size):
        pixbuf = pixbuf.scale_simple(size[0], size[1], GdkPixbuf.InterpType.BILINEAR)
    return pixbuf


def _spline_path(cr, pts):
    """Add a Catmull-Rom spline through *pts* to *cr* as cubic Béziers."""
    cr.move_to(*pts[0])
    if len(pts) == 2:
        cr.line_to(*pts[1])
        return
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        cr.curve_to(
            p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0,
            p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0,
            p2[0], p2[1],
        )


def _draw_strokes(cairo, cr, strokes, rgb_for_role):
    """Draw *strokes* opaquely with round caps, one colour per role."""
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for stroke in strokes:
        pts, radius = stroke['pts'], stroke['r']
        if not pts:
            continue
        cr.set_source_rgb(*rgb_for_role(stroke['role']))
        if len(pts) == 1:
            cr.arc(pts[0][0], pts[0][1], radius, 0, 2 * math.pi)
            cr.fill()
            continue
        _spline_path(cr, pts)
        cr.set_line_width(2 * radius)
        cr.stroke()


def _render_marks(cairo, strokes, size):
    """{'keep_mask': png, 'remove_mask': png} (white on transparent) for *strokes*."""
    masks = {}
    for role in ('keep', 'remove'):
        mine = [s for s in strokes if s['role'] == role]
        if not mine:
            continue
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size[0], size[1])
        cr = cairo.Context(surface)
        _draw_strokes(cairo, cr, mine, lambda _role: (1.0, 1.0, 1.0))
        surface.flush()
        buf = io.BytesIO()
        surface.write_to_png(buf)
        masks[f'{role}_mask'] = buf.getvalue()
    return masks


def _strokes_to_json(strokes, size):
    return json.dumps({
        'w': size[0], 'h': size[1],
        'strokes': [
            {'role': s['role'], 'r': round(s['r'], 2),
             'pts': [[round(x, 1), round(y, 1)] for x, y in s['pts']]}
            for s in strokes
        ],
    }, separators=(',', ':')).encode()


def _strokes_from_json(data, size):
    """Strokes saved by _strokes_to_json(), rescaled to *size*; [] if unreadable."""
    try:
        doc = json.loads(data)
        sx, sy = size[0] / doc['w'], size[1] / doc['h']
        out = []
        for s in doc['strokes']:
            if s['role'] not in ('keep', 'remove'):
                continue
            out.append({'role': s['role'], 'r': float(s['r']) * (sx + sy) / 2,
                        'pts': [(float(x) * sx, float(y) * sy) for x, y in s['pts']]})
        return out
    except Exception:
        return []


class SmartBrushDialog:
    """Draw-on-the-cutout Smart brush dialog.

    *photo_png* / *matte_png*: the layer and its mask, at *size*.
    *prepare(masks)*: main thread; gets {'keep_mask': png, ...} and returns a
      callable run on a worker thread (the network request); its result goes to
    *commit(result, history)*: main thread; updates GIMP and returns the new
      matte PNG for the preview.
    *stateless*: Pro — every Apply sends all strokes since the cutout
      (*history*); the local server keeps that history itself.
    """

    def __init__(self, cairo, photo_png, matte_png, size, prepare, commit,
                 stateless=False, history=None, engine_label='',
                 dialog_factory=None):
        self.cairo = cairo
        self.size = tuple(size)
        self.prepare = prepare
        self.commit = commit
        self.stateless = stateless
        self.history = list(history or [])   # applied strokes (Pro only)
        self.strokes = []                    # pending strokes
        self.current = None                  # stroke being drawn
        self.smooth = None                   # stabilised pen position
        self.pointer = None                  # widget coords, for the brush outline
        self.busy = False
        self.close_after_apply = False
        self.applies = 0

        self.photo = Gdk.cairo_surface_create_from_pixbuf(_pixbuf_from_png(photo_png, self.size), 1, None)
        self.matte = self._matte_surface(matte_png)
        self.base = None

        self._build(dialog_factory, engine_label)
        self._rebuild_base()

    # ── UI ───────────────────────────────────────────────────────────────

    def _build(self, dialog_factory, engine_label):
        title = 'WithoutBG — Smart Brush'
        self.dialog = dialog_factory(title) if dialog_factory else Gtk.Dialog(title=title)
        d = self.dialog
        d.set_resizable(True)
        self.apply_button = d.add_button('_Apply', RESPONSE_APPLY)
        self.done_button = d.add_button('_Done', Gtk.ResponseType.CLOSE)
        d.set_default_response(RESPONSE_APPLY)
        d.connect('response', self._on_response)
        d.connect('delete-event', self._on_delete)
        d.connect('key-press-event', self._on_key)

        box = d.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)

        # Row 1: brush mode, size, background opacity.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.pack_start(bar, False, False, 0)

        modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        modes.get_style_context().add_class('linked')
        self.keep_btn = Gtk.RadioButton.new(None)
        self.remove_btn = Gtk.RadioButton.new_from_widget(self.keep_btn)
        for btn, label, rgb, tip in (
            (self.keep_btn, 'Keep', BRUSH_KEEP_RGB, 'Green: paint what went missing (X to switch)'),
            (self.remove_btn, 'Remove', BRUSH_REMOVE_RGB, 'Red: paint what should go (X to switch)'),
        ):
            btn.set_mode(False)   # toggle-button look
            hexcol = '#%02x%02x%02x' % tuple(int(c * 255) for c in rgb)
            lab = Gtk.Label()
            lab.set_markup(f'<span foreground="{hexcol}">●</span>  {label}')
            btn.add(lab)
            btn.set_tooltip_text(tip)
            btn.connect('toggled', lambda *_: self.area.queue_draw())
            modes.pack_start(btn, False, False, 0)
        bar.pack_start(modes, False, False, 0)

        bar.pack_start(Gtk.Label(label='Brush size'), False, False, 0)
        self.size_adj = Gtk.Adjustment(value=BRUSH_DEFAULT_PX, lower=2, upper=300,
                                       step_increment=1, page_increment=10)
        size_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.size_adj)
        size_scale.set_draw_value(False)
        size_scale.set_size_request(120, -1)
        size_spin = Gtk.SpinButton(adjustment=self.size_adj, climb_rate=1, digits=0)
        size_spin.set_tooltip_text('Brush diameter in screen pixels ( [ and ] to change )')
        self.size_adj.connect('value-changed', lambda *_: self.area.queue_draw())
        bar.pack_start(size_scale, False, False, 0)
        bar.pack_start(size_spin, False, False, 0)

        bar.pack_start(Gtk.Label(label='Background'), False, False, 0)
        self.bg_adj = Gtk.Adjustment(value=BG_OPACITY_PCT, lower=0, upper=100,
                                     step_increment=1, page_increment=10)
        bg_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.bg_adj)
        bg_scale.set_digits(0)
        bg_scale.set_value_pos(Gtk.PositionType.RIGHT)
        bg_scale.set_size_request(140, -1)
        bg_scale.set_tooltip_text('Opacity of the removed background, so you can see what is there')
        bg_scale.connect('format-value', lambda _s, v: f'{v:.0f}%')
        self.bg_adj.connect('value-changed', lambda *_: (self._rebuild_base(), self.area.queue_draw()))
        bar.pack_start(bg_scale, True, True, 0)

        self.clear_btn = Gtk.Button(label='Clear')
        self.clear_btn.set_tooltip_text('Remove all strokes not applied yet')
        self.clear_btn.connect('clicked', lambda *_: self._clear())
        self.undo_btn = Gtk.Button(label='Undo stroke')
        self.undo_btn.set_tooltip_text('Remove the last stroke (Ctrl+Z)')
        self.undo_btn.connect('clicked', lambda *_: self._undo())
        bar.pack_end(self.clear_btn, False, False, 0)
        bar.pack_end(self.undo_btn, False, False, 0)

        # Canvas.
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        w, h = self.size
        fit = min(1.0, 860 / w, 600 / h)
        self.area.set_size_request(max(320, int(w * fit)), max(240, int(h * fit)))
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )
        self.area.connect('draw', self._on_draw)
        self.area.connect('button-press-event', self._on_press)
        self.area.connect('motion-notify-event', self._on_motion)
        self.area.connect('button-release-event', self._on_release)
        self.area.connect('leave-notify-event', self._on_leave)
        self.area.connect('scroll-event', self._on_scroll)
        self.area.connect('realize', self._on_realize)
        frame.add(self.area)
        box.pack_start(frame, True, True, 0)

        # Status line.
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        status.pack_start(self.spinner, False, False, 0)
        status.pack_start(self.status, True, True, 0)
        status.pack_end(_link_label([('How Smart Brush works', SMART_BRUSH_URL)]), False, False, 0)
        box.pack_start(status, False, False, 0)
        self._hint = (
            'Rough strokes are enough: green over what went missing, red over what should go. '
            'Right-drag paints the other colour. Apply updates the mask; Done closes.'
            + (f'  ·  {engine_label}' if engine_label else '')
        )
        self._set_status(self._hint)
        self._sync_buttons()

    def _set_status(self, text, error=False):
        if error:
            self.status.set_markup(f'<span foreground="#d33">{GLib.markup_escape_text(text)}</span>')
        else:
            self.status.set_text(text)

    def _sync_buttons(self):
        pending = bool(self.strokes)
        self.apply_button.set_sensitive(pending and not self.busy)
        self.done_button.set_sensitive(not self.busy)
        self.undo_btn.set_sensitive(pending)
        self.clear_btn.set_sensitive(pending)

    # ── Preview ──────────────────────────────────────────────────────────

    def _matte_surface(self, matte_png):
        """The mask as a cairo A8 surface (gray value → alpha)."""
        cairo = self.cairo
        pixbuf = _pixbuf_from_png(matte_png, self.size)
        w, h = self.size
        nc, rs = pixbuf.get_n_channels(), pixbuf.get_rowstride()
        pixels = pixbuf.get_pixels()
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_A8, w)
        data = bytearray(stride * h)
        for y in range(h):
            data[y * stride:y * stride + w] = pixels[y * rs:y * rs + w * nc:nc]
        return cairo.ImageSurface.create_for_data(data, cairo.FORMAT_A8, w, h, stride)

    def _rebuild_base(self):
        """Checkerboard + faded photo (background) + the cutout at full opacity."""
        cairo = self.cairo
        w, h = self.size
        base = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(base)
        tile = max(8, round(max(w, h) / 64))
        cr.set_source_rgb(0.80, 0.80, 0.80)
        cr.paint()
        cr.set_source_rgb(0.62, 0.62, 0.62)
        for ty in range(0, h, tile):
            for tx in range((ty // tile) % 2 * tile, w, 2 * tile):
                cr.rectangle(tx, ty, tile, tile)
        cr.fill()
        cr.set_source_surface(self.photo, 0, 0)
        cr.paint_with_alpha(self.bg_adj.get_value() / 100.0)
        cr.set_source_surface(self.photo, 0, 0)
        cr.mask_surface(self.matte, 0, 0)
        base.flush()
        self.base = base

    def _view(self):
        """(scale, offset_x, offset_y) mapping preview pixels to the widget."""
        aw, ah = self.area.get_allocated_width(), self.area.get_allocated_height()
        w, h = self.size
        scale = min(aw / w, ah / h, 4.0)
        return scale, (aw - w * scale) / 2, (ah - h * scale) / 2

    def _to_image(self, x, y):
        scale, ox, oy = self._view()
        return (x - ox) / scale, (y - oy) / scale

    def _role_rgb(self, role):
        return BRUSH_KEEP_RGB if role == 'keep' else BRUSH_REMOVE_RGB

    def _active_role(self, button=1):
        role = 'keep' if self.keep_btn.get_active() else 'remove'
        if button == 3:
            role = 'remove' if role == 'keep' else 'keep'
        return role

    def _on_draw(self, area, cr):
        cairo = self.cairo
        cr.set_source_rgb(0.18, 0.18, 0.18)
        cr.paint()
        scale, ox, oy = self._view()
        if scale <= 0:
            return False
        cr.save()
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        cr.set_source_surface(self.base, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        strokes = self.strokes + ([self.current] if self.current else [])
        if strokes:
            # Paint all strokes opaque in a group, then fade once: overlaps don't darken.
            cr.rectangle(0, 0, *self.size)
            cr.clip()
            cr.push_group()
            _draw_strokes(cairo, cr, strokes, self._role_rgb)
            cr.pop_group_to_source()
            cr.paint_with_alpha(BRUSH_ALPHA)
        cr.restore()

        if self.pointer is not None and not self.busy:
            x, y = self.pointer
            r = self.size_adj.get_value() / 2
            cr.set_line_width(3)
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.stroke()
            cr.set_line_width(1.5)
            role = self.current['role'] if self.current else self._active_role()
            cr.set_source_rgb(*self._role_rgb(role))
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.stroke()
        return False

    # ── Painting ─────────────────────────────────────────────────────────

    def _on_realize(self, area):
        try:
            area.get_window().set_cursor(Gdk.Cursor.new_from_name(area.get_display(), 'crosshair'))
        except Exception:
            pass

    def _on_press(self, area, event):
        area.grab_focus()
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button not in (1, 3):
            return False
        scale, _ox, _oy = self._view()
        p = self._to_image(event.x, event.y)
        self.smooth = p
        self.current = {'role': self._active_role(event.button),
                        'r': self.size_adj.get_value() / 2 / scale, 'pts': [p]}
        area.queue_draw()
        return True

    def _on_motion(self, area, event):
        self.pointer = (event.x, event.y)
        if self.current is not None:
            # Stabiliser: the pen trails the pointer, which irons out jitter.
            px, py = self._to_image(event.x, event.y)
            sx, sy = self.smooth
            sx += (px - sx) * BRUSH_SMOOTHING
            sy += (py - sy) * BRUSH_SMOOTHING
            self.smooth = (sx, sy)
            lx, ly = self.current['pts'][-1]
            scale, _ox, _oy = self._view()
            if math.hypot(sx - lx, sy - ly) * scale >= 3.0:
                self.current['pts'].append((sx, sy))
        area.queue_draw()
        return True

    def _on_release(self, area, event):
        if self.current is None:
            return False
        stroke, self.current = self.current, None
        if self.smooth is not None and len(stroke['pts']) > 1:
            stroke['pts'].append(self.smooth)
        self.strokes.append(stroke)
        self._set_status(f'{len(self.strokes)} stroke(s) ready. Apply to update the mask.')
        self._sync_buttons()
        area.queue_draw()
        return True

    def _on_leave(self, area, event):
        self.pointer = None
        area.queue_draw()
        return False

    def _on_scroll(self, area, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        ok, dx, dy = event.get_scroll_deltas()
        if not ok:
            dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(event.direction, 0)
        self._resize_brush(1.0 / 1.15 if dy > 0 else 1.15)
        return True

    def _resize_brush(self, factor):
        self.size_adj.set_value(round(self.size_adj.get_value() * factor) or 2)

    def _on_key(self, widget, event):
        if isinstance(self.dialog.get_focus(), Gtk.Entry):
            return False
        key = Gdk.keyval_name(event.keyval) or ''
        if event.state & Gdk.ModifierType.CONTROL_MASK and key.lower() == 'z':
            self._undo()
            return True
        if key.lower() == 'x':
            (self.remove_btn if self.keep_btn.get_active() else self.keep_btn).set_active(True)
            return True
        if key == 'bracketleft':
            self._resize_brush(1.0 / 1.2)
            return True
        if key == 'bracketright':
            self._resize_brush(1.2)
            return True
        return False

    def _undo(self):
        if self.strokes:
            self.strokes.pop()
            self._sync_buttons()
            self.area.queue_draw()

    def _clear(self):
        self.strokes = []
        self._sync_buttons()
        self.area.queue_draw()

    # ── Apply / Done ─────────────────────────────────────────────────────

    def _apply(self):
        if self.busy or not self.strokes:
            return
        sent = list(self.strokes)
        to_send = self.history + sent if self.stateless else sent
        try:
            work = self.prepare(_render_marks(self.cairo, to_send, self.size))
        except Exception as exc:
            self._finish(sent, None, exc)
            return
        self.busy = True
        self.spinner.start()
        self._set_status('Applying Smart brush…')
        self._sync_buttons()
        self.area.queue_draw()

        def run():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(self._finish, sent, result, error)

        threading.Thread(target=run, daemon=True).start()

    def _finish(self, sent, result, error):
        self.busy = False
        self.spinner.stop()
        if error is None:
            history = self.history + sent if self.stateless else []
            try:
                matte_png = self.commit(result, history)
                self.matte = self._matte_surface(matte_png)
                self._rebuild_base()
            except Exception as exc:
                error = exc
            else:
                self.history = history
                # Strokes painted while the request ran stay pending.
                self.strokes = self.strokes[len(sent):]
                self.applies += 1
                self._set_status('Mask updated. Keep painting and Apply again, or Done to close.')
        if error is not None:
            self.close_after_apply = False
            self._set_status(f'Apply failed: {error}', error=True)
        self._sync_buttons()
        self.area.queue_draw()
        if self.close_after_apply and error is None:
            self._close()
        return False

    def _confirm_close(self):
        """True when the dialog may close now (asks about unapplied strokes)."""
        if not self.strokes:
            return True
        msg = Gtk.MessageDialog(
            transient_for=self.dialog, modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
            text=f'Apply {len(self.strokes)} unapplied stroke(s) before closing?',
        )
        msg.add_buttons('_Discard', Gtk.ResponseType.NO, '_Cancel', Gtk.ResponseType.CANCEL,
                        '_Apply', Gtk.ResponseType.YES)
        msg.set_default_response(Gtk.ResponseType.YES)
        answer = msg.run()
        msg.destroy()
        if answer == Gtk.ResponseType.YES:
            self.close_after_apply = True
            self._apply()
            return False
        return answer == Gtk.ResponseType.NO

    def _on_response(self, dialog, response):
        if response == RESPONSE_APPLY:
            self._apply()
        elif response in (Gtk.ResponseType.CLOSE, Gtk.ResponseType.DELETE_EVENT):
            if not self.busy and self._confirm_close():
                self._close()

    def _on_delete(self, dialog, event):
        self._on_response(dialog, Gtk.ResponseType.DELETE_EVENT)
        return True   # never destroy here; _close() hides and quits

    def _close(self):
        self.dialog.hide()
        Gtk.main_quit()

    def run(self):
        self.dialog.show_all()
        self.area.grab_focus()
        Gtk.main()
        self.dialog.destroy()
        return self.applies


# ── Procedure callback ────────────────────────────────────────────────────────

def run_remove_background(procedure, run_mode, image, drawables, config, data):
    """
    Main entry point called by GIMP.

    Signature must match Gimp.ImageProcedure callback:
      (procedure, run_mode, image, drawables, config, data)
    Note: no separate n_drawables — GIMP 3 Python passes a plain list.
    """
    try:
        server_url, use_pro, api_key = _read_config(config)

        target = _resolve_target(image, drawables)
        if target is None:
            Gimp.message('WithoutBG error:\n\nNo active layer to operate on.')
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error()
            )

        # ── Interactive dialog ──────────────────────────────────────────────
        if run_mode == Gimp.RunMode.INTERACTIVE:
            GimpUi.init('withoutbg.py')

            dialog = GimpUi.ProcedureDialog.new(
                procedure, config,
                'WithoutBG — Remove Background',
            )
            hint = GimpUi.HintBox.new(
                'Adds the cutout as an unapplied layer mask, in place.\n'
                'Review it, then Layer ▸ Mask ▸ Apply Layer Mask to commit.\n\n'
                f'Default server: {SERVER_URL} (Docker service-* or Mac server app).\n'
                + _server_status_markup(server_url) + '\n\n'
                'Optional: withoutBG Pro sends the layer to api.withoutbg.com\n'
                '(1 credit per image, needs an API key).\n\n'
                'To fix the mask: Tools ▸ WithoutBG ▸ Smart Brush… (local server; Pro cutouts\n'
                'need WITHOUTBG_API_KEY in the environment).',
            )
            dialog.get_content_area().add(hint)
            hint.show()
            links = _link_label([
                ('Setup guide: local server, withoutBG Pro and Smart Brush', PLUGIN_DOCS_URL),
            ])
            dialog.get_content_area().add(links)
            links.show()

            _mask_api_key_entry(dialog)
            dialog.fill(['server-url', 'use-pro', 'api-key'])

            if not dialog.run():
                dialog.destroy()
                return procedure.new_return_values(
                    Gimp.PDBStatusType.CANCEL, GLib.Error()
                )
            dialog.destroy()

            server_url, use_pro, api_key = _read_config(config)

        if use_pro and not api_key:
            Gimp.message(
                'WithoutBG error:\n\n'
                'withoutBG Pro needs an API key: enter it in the dialog or set '
                f'WITHOUTBG_API_KEY.\n\nGet one at {PRO_KEY_URL}'
            )
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error()
            )

        ok, info = (True, None) if use_pro else _health_check(server_url)
        if not ok:
            Gimp.message(
                'WithoutBG error:\n\n'
                f'Cannot reach the WithoutBG server at {server_url}.\n\n{info}'
            )
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error()
            )

        # ── Processing ────────────────────────────────────────────────────
        tmp_in  = tempfile.mktemp(suffix='_wbg_in.png')
        tmp_out = tempfile.mktemp(suffix='_wbg_out.png')

        try:
            # 1 — export the full target layer (longest side ≤ MAX_SERVER_SIDE)
            Gimp.progress_init('WithoutBG: preparing image…')
            Gimp.progress_update(0.10)
            _export_layer_png(image, target, tmp_in, max_side=MAX_SERVER_SIDE)

            with open(tmp_in, 'rb') as fh:
                png_bytes = fh.read()
            if not png_bytes:
                raise RuntimeError('Exported PNG is empty — export failed.')

            # 2 — send to server.
            # Always request the *matte* (single-channel alpha): the server caps its
            # output at 1024 px, so fetching the cutout RGBA directly would lose
            # resolution. We instead upscale the matte locally and apply it to the
            # full-resolution image — no quality loss.
            Gimp.progress_init('WithoutBG: removing background…')
            Gimp.progress_update(0.35)
            if use_pro:
                result_bytes, latency_ms = _post_pro(png_bytes, api_key)
            else:
                result_bytes, latency_ms = _post_image(png_bytes, server_url, 'matte')

            # 3 — load the matte and add it as a layer mask at full resolution
            Gimp.progress_init('WithoutBG: applying result…')
            Gimp.progress_update(0.80)

            with open(tmp_out, 'wb') as fh:
                fh.write(result_bytes)

            matte_img = Gimp.file_load(
                Gimp.RunMode.NONINTERACTIVE,
                Gio.File.new_for_path(tmp_out),
            )
            if matte_img is None:
                raise RuntimeError('GIMP could not load the matte PNG.')

            image.undo_group_start()
            try:
                # Attach the upscaled matte to the target layer as an unapplied mask.
                _add_matte_mask(image, target, matte_img)
                matte_img.delete()
                # A new cutout: Smart brush starts from it with a fresh context.
                _clear_parasite(target, CONTEXT_PARASITE)
                _clear_parasite(target, STROKES_PARASITE)
                # Remember which engine made it; Smart brush Apply follows it.
                _set_parasite_bytes(target, ENGINE_PARASITE, b'pro' if use_pro else b'open')
                if use_pro:
                    # Pro strokes are the history of this cutout only: start empty.
                    for layer in _paint_layers(image, target).values():
                        layer.edit_clear()
            finally:
                image.undo_group_end()
            if not use_pro:
                _remember_server(server_url)

            Gimp.displays_flush()
            Gimp.progress_update(1.0)

        except Exception as exc:
            Gimp.message(f'WithoutBG error:\n\n{exc}')
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error()
            )
        finally:
            for p in (tmp_in, tmp_out):
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

    except Exception as exc:
        Gimp.message(
            'WithoutBG error:\n\n'
            f'{exc}\n\n{traceback.format_exc()}'
        )
        return procedure.new_return_values(
            Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error()
        )


def _fail(procedure, message):
    Gimp.message(f'WithoutBG Smart brush:\n\n{message}')
    return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error())


def run_add_paint_layers(procedure, run_mode, image, drawables, config, data):
    """Add 'withoutBG Keep' / 'withoutBG Remove' paint layers above the target."""
    try:
        target = _smart_brush_target(image, drawables)
        if target is None:
            return _fail(procedure, 'Select the layer you removed the background from.')

        existing = _paint_layers(image, target)
        image.undo_group_start()
        try:
            parent = target.get_parent()
            for role, name, tag in (
                ('remove', REMOVE_LAYER_NAME, Gimp.ColorTag.RED),
                ('keep', KEEP_LAYER_NAME, Gimp.ColorTag.GREEN),
            ):
                if role in existing:
                    continue
                layer = Gimp.Layer.new(
                    image, name, target.get_width(), target.get_height(),
                    _paint_layer_type(image), 60.0, Gimp.LayerMode.NORMAL,
                )
                image.insert_layer(layer, parent, image.get_item_position(target))
                ok, ox, oy = target.get_offsets()
                layer.set_offsets(ox, oy)
                layer.fill(Gimp.FillType.TRANSPARENT)
                layer.set_color_tag(tag)
                _set_parasite_bytes(layer, PAINT_PARASITE, f'{role}:{target.get_tattoo()}'.encode())
                existing[role] = layer
        finally:
            image.undo_group_end()

        image.set_selected_layers([existing['keep']])
        Gimp.displays_flush()
        if run_mode == Gimp.RunMode.INTERACTIVE:
            Gimp.message(
                'Smart brush: paint with any brush at full opacity.\n\n'
                f'• "{KEEP_LAYER_NAME}" (green tag): what went missing\n'
                f'• "{REMOVE_LAYER_NAME}" (red tag): what should go\n\n'
                'One stroke on an object is enough. Then Tools ▸ WithoutBG ▸ '
                'Smart Brush: Apply.'
            )
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    except Exception as exc:
        return _fail(procedure, f'{exc}\n\n{traceback.format_exc()}')


def _smart_brush_apply_pro(procedure, image, target, paint, painted):
    """Smart brush Apply on a Pro cutout: the photo plus every stroke so far (1 credit).

    Stateless on the server, so the paint layers are not cleared: each Apply sends
    all strokes painted since the cutout was made. Needs WITHOUTBG_API_KEY (the
    Smart Brush actions have no key field).
    """
    api_key = os.environ.get('WITHOUTBG_API_KEY', '').strip()
    if not api_key:
        return _fail(procedure, 'Smart brush on a withoutBG Pro cutout needs the API key in '
                                'WITHOUTBG_API_KEY. Set it and restart GIMP.')

    size = _fit_longest_side(target.get_width(), target.get_height())
    tmp = {k: tempfile.mktemp(suffix=f'_wbg_{k}.png') for k in ('image', 'keep', 'remove', 'out')}
    try:
        Gimp.progress_init('WithoutBG: Smart brush (Pro)…')
        Gimp.progress_update(0.1)
        _export_layer_png(image, target, tmp['image'], max_side=MAX_SERVER_SIDE)
        with open(tmp['image'], 'rb') as fh:
            png_bytes = fh.read()
        masks = {}
        for role, layer in painted.items():
            _export_marks_png(image, layer, tmp[role], size)
            with open(tmp[role], 'rb') as fh:
                masks[f'{role}_mask'] = fh.read()

        Gimp.progress_update(0.4)
        result_bytes, _latency = _post_pro(png_bytes, api_key, masks)

        Gimp.progress_update(0.8)
        with open(tmp['out'], 'wb') as fh:
            fh.write(result_bytes)
        matte_img = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(tmp['out']))
        if matte_img is None:
            raise RuntimeError('GIMP could not load the matte PNG.')

        image.undo_group_start()
        try:
            _add_matte_mask(image, target, matte_img)
            matte_img.delete()
        finally:
            image.undo_group_end()
        Gimp.displays_flush()
        Gimp.progress_update(1.0)
    finally:
        for path in tmp.values():
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


def _apply_matte_bytes(image, target, matte_png):
    """Load *matte_png* into *target*'s layer mask (one undo step)."""
    path = tempfile.mktemp(suffix='_wbg_matte.png')
    try:
        with open(path, 'wb') as fh:
            fh.write(matte_png)
        matte_img = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(path))
        if matte_img is None:
            raise RuntimeError('GIMP could not load the matte PNG.')
        image.undo_group_start()
        try:
            _add_matte_mask(image, target, matte_img)
        finally:
            image.undo_group_end()
            matte_img.delete()
        Gimp.displays_flush()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_temp_png(export):
    """Call *export(path)* with a temp PNG path and return the file's bytes."""
    path = tempfile.mktemp(suffix='_wbg.png')
    try:
        export(path)
        with open(path, 'rb') as fh:
            return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run_smart_brush_dialog(procedure, run_mode, image, drawables, config, data):
    """Smart brush: paint Keep / Remove strokes on a preview, Apply updates the mask."""
    try:
        target = _smart_brush_target(image, drawables)
        if target is None:
            return _fail(procedure, 'Select the layer you removed the background from.')
        if target.get_mask() is None:
            return _fail(procedure, 'This layer has no mask yet. Run Tools ▸ WithoutBG ▸ '
                                    'Remove Background first.')
        cairo = _load_cairo()
        if cairo is None:
            return _fail(procedure, 'The Smart Brush dialog needs pycairo, which this GIMP '
                                    'build lacks. Use Smart Brush: Add Paint Layers / Apply instead.')

        pro = _get_parasite_bytes(target, ENGINE_PARASITE) == b'pro'
        api_key = os.environ.get('WITHOUTBG_API_KEY', '').strip()
        server_url = _remembered_server()
        if pro:
            if not api_key:
                return _fail(procedure, 'Smart brush on a withoutBG Pro cutout needs the API key in '
                                        'WITHOUTBG_API_KEY. Set it and restart GIMP.')
            engine_label = 'withoutBG Pro: 1 credit per Apply'
        else:
            ok, info = _health_check(server_url)
            if not ok:
                return _fail(procedure, f'Smart brush runs on the local open-weights server.\n'
                                        f'Cannot reach it at {server_url}.\n\n{info}')
            engine_label = f'Local server {server_url}'

        size = _fit_longest_side(target.get_width(), target.get_height())
        photo_png = _read_temp_png(lambda p: _export_layer_png(image, target, p, MAX_SERVER_SIDE))
        matte_png = _read_temp_png(lambda p: _export_mask_png(target, p, size))
        history = []
        if pro:
            saved = _get_parasite_bytes(target, STROKES_PARASITE)
            history = _strokes_from_json(saved, size) if saved else []

        if pro:
            def prepare(masks):
                return lambda: _post_pro(photo_png, api_key, masks)[0]

            def commit(result, all_strokes):
                _apply_matte_bytes(image, target, result)
                _set_parasite_bytes(target, STROKES_PARASITE, _strokes_to_json(all_strokes, size))
                return result
        else:
            def prepare(masks):
                # Start from the mask as it is now: hand edits since the last Apply count.
                body = {
                    'image': 'data:image/png;base64,' + base64.b64encode(photo_png).decode('ascii'),
                    'matte': 'data:image/png;base64,' + base64.b64encode(
                        _read_temp_png(lambda p: _export_mask_png(target, p, size))).decode('ascii'),
                }
                for name, png in masks.items():
                    body[name] = 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
                context = _get_parasite_bytes(target, CONTEXT_PARASITE)
                if context:
                    body['edit_context'] = context.decode('ascii', 'replace')
                return lambda: _post_json(server_url, body)

            def commit(reply, _history):
                matte = _decode_data_url(reply['alphaMatte'])
                _apply_matte_bytes(image, target, matte)
                if reply.get('editContext'):
                    _set_parasite_bytes(target, CONTEXT_PARASITE, reply['editContext'].encode('ascii'))
                return matte

        GimpUi.init('withoutbg.py')

        def make_dialog(title):
            dialog = GimpUi.Dialog(title=title, role='withoutbg-smart-brush', use_header_bar=False)
            try:
                GimpUi.window_set_transient(dialog)
            except Exception:
                pass
            return dialog

        SmartBrushDialog(
            cairo, photo_png, matte_png, size, prepare, commit,
            stateless=pro, history=history, engine_label=engine_label,
            dialog_factory=make_dialog,
        ).run()
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    except Exception as exc:
        return _fail(procedure, f'{exc}\n\n{traceback.format_exc()}')


def run_smart_brush_apply(procedure, run_mode, image, drawables, config, data):
    """Send the paint to the local server and update the layer mask in place."""
    try:
        target = _smart_brush_target(image, drawables)
        if target is None:
            return _fail(procedure, 'Select the layer you removed the background from.')
        if target.get_mask() is None:
            return _fail(procedure, 'This layer has no mask yet. Run Tools ▸ WithoutBG ▸ '
                                    'Remove Background first.')
        paint = _paint_layers(image, target)
        painted = {role: layer for role, layer in paint.items() if _is_painted(layer)}
        if not painted:
            return _fail(procedure, 'Nothing painted yet. Use Smart Brush: Add Paint Layers, '
                                    f'then paint on "{KEEP_LAYER_NAME}" or "{REMOVE_LAYER_NAME}".')

        if _get_parasite_bytes(target, ENGINE_PARASITE) == b'pro':
            return _smart_brush_apply_pro(procedure, image, target, paint, painted)

        server_url = _remembered_server()
        ok, info = _health_check(server_url)
        if not ok:
            return _fail(procedure, f'Smart brush runs on the local open-weights server.\n'
                                    f'Cannot reach it at {server_url}.\n\n{info}')

        size = _fit_longest_side(target.get_width(), target.get_height())
        tmp = {k: tempfile.mktemp(suffix=f'_wbg_{k}.png')
               for k in ('image', 'matte', 'keep', 'remove', 'out')}
        try:
            Gimp.progress_init('WithoutBG: Smart brush…')
            Gimp.progress_update(0.1)
            _export_layer_png(image, target, tmp['image'], max_side=MAX_SERVER_SIDE)
            _export_mask_png(target, tmp['matte'], size)
            body = {
                'image': _png_data_url(tmp['image']),
                'matte': _png_data_url(tmp['matte']),
            }
            for role, layer in painted.items():
                _export_marks_png(image, layer, tmp[role], size)
                body[f'{role}_mask'] = _png_data_url(tmp[role])
            context = _get_parasite_bytes(target, CONTEXT_PARASITE)
            if context:
                body['edit_context'] = context.decode('ascii', 'replace')

            Gimp.progress_update(0.4)
            reply = _post_json(server_url, body)

            Gimp.progress_update(0.8)
            with open(tmp['out'], 'wb') as fh:
                fh.write(_decode_data_url(reply['alphaMatte']))
            matte_img = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE, Gio.File.new_for_path(tmp['out']))
            if matte_img is None:
                raise RuntimeError('GIMP could not load the matte PNG.')

            image.undo_group_start()
            try:
                _add_matte_mask(image, target, matte_img)
                matte_img.delete()
                for layer in paint.values():
                    layer.edit_clear()
            finally:
                image.undo_group_end()
            # The server's editContext lets the next Apply skip the full pipeline.
            if reply.get('editContext'):
                _set_parasite_bytes(target, CONTEXT_PARASITE, reply['editContext'].encode('ascii'))
            Gimp.displays_flush()
            Gimp.progress_update(1.0)
        finally:
            for path in tmp.values():
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError:
                    pass
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())
    except Exception as exc:
        return _fail(procedure, f'{exc}\n\n{traceback.format_exc()}')


# ── Registration ──────────────────────────────────────────────────────────────

class WithoutBGPlugin(Gimp.PlugIn):

    def do_set_i18n(self, procname):
        return False, None, None

    def do_query_procedures(self):
        return [
            'withoutbg-remove-background',
            'withoutbg-smart-brush',
            'withoutbg-smart-brush-layers',
            'withoutbg-smart-brush-apply',
        ]

    def do_create_procedure(self, name):
        if name == 'withoutbg-smart-brush':
            return self._smart_brush_procedure(
                name, run_smart_brush_dialog, 'Smart Brush…',
                'Fix the layer mask by painting on a preview',
                'Opens a dialog showing the cutout: paint rough green (keep) and '
                'red (remove) strokes, Apply to update the layer mask in place, '
                'Done to close.',
            )
        if name == 'withoutbg-smart-brush-layers':
            return self._smart_brush_procedure(
                name, run_add_paint_layers, 'Smart Brush: Add Paint Layers',
                'Add Smart brush paint layers',
                'Adds "withoutBG Keep" and "withoutBG Remove" layers above the '
                'layer: paint what went missing and what should go, then run '
                'Smart Brush: Apply.',
            )
        if name == 'withoutbg-smart-brush-apply':
            return self._smart_brush_procedure(
                name, run_smart_brush_apply, 'Smart Brush: Apply',
                'Fix the layer mask from Smart brush paint',
                'Sends the layer, its mask and the Keep / Remove paint to the '
                'local WithoutBG server (open weights) and updates the mask in '
                'place. Later applies on the same layer run only the Smart '
                'brush graph.',
            )
        proc = Gimp.ImageProcedure.new(
            self, name,
            Gimp.PDBProcType.PLUGIN,
            run_remove_background, None,
        )
        proc.set_image_types('*')
        proc.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE)
        proc.set_menu_label('Remove Background…')
        proc.set_documentation(
            'Remove background via local WithoutBG server or withoutBG Pro',
            'Sends the active layer to a local WithoutBG server (Docker service-* '
            'or Mac app on port 8000), fetches the alpha matte, and adds it as an '
            'unapplied layer mask.',
            name,
        )
        proc.set_attribution('WithoutBG', 'WithoutBG', '2026')
        proc.add_menu_path('<Image>/Tools/WithoutBG')

        proc.add_string_argument(
            'server-url', 'Server URL',
            'URL of the local WithoutBG server (default port 8000)',
            SERVER_URL,
            GObject.ParamFlags.READWRITE,
        )
        proc.add_boolean_argument(
            'use-pro', 'Use withoutBG Pro (cloud)',
            'Send the layer to the hosted withoutBG Pro API instead of the local '
            'server. Needs an API key; 1 credit per image.',
            False,
            GObject.ParamFlags.READWRITE,
        )
        proc.add_string_argument(
            'api-key', 'withoutBG Pro API key',
            'Only used with withoutBG Pro. Empty: read WITHOUTBG_API_KEY.',
            '',
            GObject.ParamFlags.READWRITE,
        )

        return proc

    def _smart_brush_procedure(self, name, callback, label, blurb, help_text):
        proc = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, callback, None)
        proc.set_image_types('*')
        proc.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE)
        proc.set_menu_label(label)
        proc.set_documentation(blurb, help_text, name)
        proc.set_attribution('WithoutBG', 'WithoutBG', '2026')
        proc.add_menu_path('<Image>/Tools/WithoutBG')
        return proc


Gimp.main(WithoutBGPlugin.__gtype__, sys.argv)
