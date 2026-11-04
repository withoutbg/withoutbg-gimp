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
from gi.repository import Gimp, GimpUi, Gtk, GObject, GLib, Gio, Gegl

import os
import sys
import json
import base64
import traceback
import tempfile
import uuid
import urllib.request
import urllib.error

# ── Configuration ────────────────────────────────────────────────────────────

PLUGIN_VERSION = "1.2.0"

SERVER_URL = "http://127.0.0.1:8000"

# withoutBG Pro (opt-in). Key from the dialog, or the WITHOUTBG_API_KEY env var.
PRO_API_URL = os.environ.get("WITHOUTBG_API_URL", "https://api.withoutbg.com").rstrip("/")
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
SERVER_PARASITE   = 'withoutbg-server-url'     # global: last local server used

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


def _post_pro(png_bytes, api_key):
    """POST *png_bytes* to withoutBG Pro; returns (matte_png_bytes, latency_str)."""
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\n'
        'Content-Disposition: form-data; name="file"; filename="image.png"\r\n'
        'Content-Type: image/png\r\n\r\n'
    ).encode() + png_bytes + f'\r\n--{boundary}--\r\n'.encode()
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
                f'(1 credit per image). API key: {PRO_KEY_URL}\n\n'
                'To fix the mask: Tools ▸ WithoutBG ▸ Smart Brush (local server).',
            )
            dialog.get_content_area().add(hint)
            hint.show()

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
            'withoutbg-smart-brush-layers',
            'withoutbg-smart-brush-apply',
        ]

    def do_create_procedure(self, name):
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
