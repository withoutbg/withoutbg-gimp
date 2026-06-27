#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# WithoutBG — GIMP 3 Plugin
# Removes image backgrounds using a local WithoutBG server (Docker or Mac app).
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
#
# License: GNU General Public License v3 or later
# <https://www.gnu.org/licenses/>

import gi
gi.require_version('Gimp',   '3.0')
gi.require_version('GimpUi', '3.0')
gi.require_version('Gtk',    '3.0')
from gi.repository import Gimp, GimpUi, Gtk, GObject, GLib, Gio

import os
import sys
import json
import traceback
import tempfile
import urllib.request
import urllib.error

# ── Configuration ────────────────────────────────────────────────────────────

SERVER_URL = "http://127.0.0.1:8000"

# The server downsamples input so the longest side is at most this many pixels.
# We match that locally before POSTing to avoid 413 payloads and to keep the
# returned matte aligned with what the model actually sees.
MAX_SERVER_SIDE = 1024

# ── Internals ─────────────────────────────────────────────────────────────────

def _fit_longest_side(width, height, max_side=MAX_SERVER_SIDE):
    """Return (w, h) with longest side ≤ *max_side*, preserving aspect ratio."""
    scale = min(max_side / width, max_side / height, 1.0)
    if scale >= 1.0:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))

def _health_check(server_url):
    """Return (ok: bool, info: dict | str)."""
    try:
        with urllib.request.urlopen(f"{server_url}/health", timeout=5) as r:
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
    req = urllib.request.Request(url, data=png_bytes, method='POST')
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
        # Attach a white mask to the existing layer and copy the matte into it.
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


# ── Procedure callback ────────────────────────────────────────────────────────

def run_remove_background(procedure, run_mode, image, drawables, config, data):
    """
    Main entry point called by GIMP.

    Signature must match Gimp.ImageProcedure callback:
      (procedure, run_mode, image, drawables, config, data)
    Note: no separate n_drawables — GIMP 3 Python passes a plain list.
    """
    try:
        try:
            server_url = config.get_property('server-url') or SERVER_URL
        except Exception:
            server_url = SERVER_URL

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
                + _server_status_markup(server_url),
            )
            dialog.get_content_area().add(hint)
            hint.show()

            dialog.fill(['server-url'])

            if not dialog.run():
                dialog.destroy()
                return procedure.new_return_values(
                    Gimp.PDBStatusType.CANCEL, GLib.Error()
                )
            dialog.destroy()

            try:
                server_url = config.get_property('server-url') or SERVER_URL
            except Exception:
                server_url = SERVER_URL

        ok, info = _health_check(server_url)
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
            finally:
                image.undo_group_end()

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


# ── Registration ──────────────────────────────────────────────────────────────

class WithoutBGPlugin(Gimp.PlugIn):

    def do_set_i18n(self, procname):
        return False, None, None

    def do_query_procedures(self):
        return ['withoutbg-remove-background']

    def do_create_procedure(self, name):
        proc = Gimp.ImageProcedure.new(
            self, name,
            Gimp.PDBProcType.PLUGIN,
            run_remove_background, None,
        )
        proc.set_image_types('*')
        proc.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE)
        proc.set_menu_label('Remove Background…')
        proc.set_documentation(
            'Remove background via local WithoutBG server',
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

        return proc


Gimp.main(WithoutBGPlugin.__gtype__, sys.argv)
