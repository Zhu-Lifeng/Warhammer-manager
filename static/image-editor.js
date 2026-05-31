/*
 * Image editor — modal-based crop + 90° rotate.
 *
 * Two entry points:
 *   1. Pre-upload: any <input type="file" data-edit-on-upload> triggers the
 *      editor on `change`. Edited blob replaces the file in the input so the
 *      form upload sends the edited version.
 *   2. Post-upload: any element with data-edit-image="<src>" data-edit-post="<url>"
 *      opens the editor on click; on Apply, POSTs multipart {image: blob}
 *      to <url> and reloads on success.
 *
 * Uses Pointer Events for unified touch + mouse + pen input. The modal
 * locks page scroll while open via overflow:hidden on body.
 */
(function () {
  "use strict";

  const MAX_EDGE = 2400;          // downscale very large source images first
  const MIN_CROP = 16;            // minimum crop edge in source pixels

  // -------------------- modal lifecycle --------------------

  let openResolver = null;        // call with Blob|null when modal closes

  function openEditor(imageBitmap) {
    return new Promise(function (resolve) {
      openResolver = resolve;
      buildModal(imageBitmap);
    });
  }

  function closeModal(result) {
    document.body.classList.remove("ie-modal-open");
    const m = document.getElementById("ie-modal");
    if (m) m.remove();
    if (openResolver) {
      const r = openResolver; openResolver = null;
      r(result);
    }
  }

  function buildModal(srcBitmap) {
    document.body.classList.add("ie-modal-open");
    const modal = document.createElement("div");
    modal.id = "ie-modal";
    modal.innerHTML = `
      <div class="ie-instructions">Drag on the image to crop.</div>
      <div class="ie-toolbar">
        <button type="button" data-act="pick" class="ie-btn">Choose file…</button>
        <button type="button" data-act="rotL" class="ie-btn" title="Rotate left">↺</button>
        <button type="button" data-act="rotR" class="ie-btn" title="Rotate right">↻</button>
        <button type="button" data-act="reset" class="ie-btn-ghost">Reset</button>
        <span class="ie-spacer"></span>
        <button type="button" data-act="cancel" class="ie-btn-ghost">Cancel</button>
        <button type="button" data-act="apply" class="ie-btn-primary" ${srcBitmap ? '' : 'disabled'}>Apply</button>
      </div>
      <div class="ie-stage">
        ${srcBitmap ? '' : '<div class="ie-empty">No image yet — tap <b>Choose file…</b> to pick one or take a photo.</div>'}
        <div class="ie-canvas-wrap" ${srcBitmap ? '' : 'hidden'}>
          <canvas class="ie-canvas"></canvas>
          <div class="ie-sel" hidden>
            <div class="ie-handle" data-h="nw"></div>
            <div class="ie-handle" data-h="ne"></div>
            <div class="ie-handle" data-h="sw"></div>
            <div class="ie-handle" data-h="se"></div>
          </div>
        </div>
      </div>
      <input type="file" accept="image/*" class="ie-pick" hidden>
    `;
    document.body.appendChild(modal);

    const canvas = modal.querySelector(".ie-canvas");
    const wrap   = modal.querySelector(".ie-canvas-wrap");
    const sel    = modal.querySelector(".ie-sel");

    // State: source bitmap, rotation in 90° steps, crop rect in current
    // (rotated) source coordinate space, or null when no crop selected.
    const state = {
      src: srcBitmap,
      rotation: 0,      // 0 | 90 | 180 | 270
      crop: null,       // {x, y, w, h} in rotated-source pixels
    };

    function rotatedSize() {
      if (!state.src) return { w: 0, h: 0 };
      const w = state.src.width, h = state.src.height;
      return (state.rotation % 180 === 0) ? {w, h} : {w: h, h: w};
    }

    // Draw the source bitmap onto the canvas after the current rotation.
    function redrawBase() {
      if (!state.src) return;
      const { w, h } = rotatedSize();
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.save();
      ctx.translate(w / 2, h / 2);
      ctx.rotate((state.rotation * Math.PI) / 180);
      // Original bitmap is drawn centred at its own size; the rotation around
      // the (rotated-canvas) centre puts it in the right place.
      ctx.drawImage(state.src, -state.src.width / 2, -state.src.height / 2);
      ctx.restore();
      layoutSel();
    }

    async function loadNewSource(file) {
      let bmp;
      try {
        bmp = await fileToBitmap(file);
      } catch (_) {
        alert("Could not load image.");
        return;
      }
      state.src = downscaleIfHuge(bmp);
      state.rotation = 0;
      state.crop = null;
      // Reveal the canvas + remove empty-state hint on first load.
      const empty = modal.querySelector(".ie-empty");
      if (empty) empty.remove();
      wrap.hidden = false;
      modal.querySelector('[data-act="apply"]').disabled = false;
      redrawBase();
    }

    // Map between source coords (canvas pixels) and CSS display coords.
    function scale() {
      const r = canvas.getBoundingClientRect();
      return r.width / canvas.width;
    }

    function layoutSel() {
      if (!state.crop) { sel.hidden = true; return; }
      sel.hidden = false;
      const s = scale();
      sel.style.left   = (state.crop.x * s) + "px";
      sel.style.top    = (state.crop.y * s) + "px";
      sel.style.width  = (state.crop.w * s) + "px";
      sel.style.height = (state.crop.h * s) + "px";
    }

    function clampRect(r) {
      const { w, h } = rotatedSize();
      const x = Math.max(0, Math.min(w - 1, r.x));
      const y = Math.max(0, Math.min(h - 1, r.y));
      const wv = Math.max(MIN_CROP, Math.min(w - x, r.w));
      const hv = Math.max(MIN_CROP, Math.min(h - y, r.h));
      return { x, y, w: wv, h: hv };
    }

    // -------------------- pointer drag --------------------

    let drag = null;   // {mode: 'new'|'move'|'resize', handle, startRect, startPoint, pid}

    function pointToSource(e) {
      const box = canvas.getBoundingClientRect();
      const s = scale();
      const { w, h } = rotatedSize();
      return {
        x: Math.max(0, Math.min(w, (e.clientX - box.left) / s)),
        y: Math.max(0, Math.min(h, (e.clientY - box.top)  / s)),
      };
    }

    function startDrag(e, mode, handle) {
      e.preventDefault();
      const p = pointToSource(e);
      drag = {
        mode,
        handle,
        startPoint: p,
        startRect: state.crop ? { ...state.crop } : null,
        pid: e.pointerId,
      };
      try { e.target.setPointerCapture(e.pointerId); } catch (_) {}
      if (mode === "new") {
        state.crop = clampRect({ x: p.x, y: p.y, w: MIN_CROP, h: MIN_CROP });
        layoutSel();
      }
    }

    function continueDrag(e) {
      if (!drag || (drag.pid !== undefined && e.pointerId !== drag.pid)) return;
      const p = pointToSource(e);
      const r0 = drag.startRect;
      let nr;
      if (drag.mode === "new") {
        const sx = drag.startPoint.x, sy = drag.startPoint.y;
        nr = {
          x: Math.min(sx, p.x),
          y: Math.min(sy, p.y),
          w: Math.max(MIN_CROP, Math.abs(p.x - sx)),
          h: Math.max(MIN_CROP, Math.abs(p.y - sy)),
        };
      } else if (drag.mode === "move") {
        const dx = p.x - drag.startPoint.x;
        const dy = p.y - drag.startPoint.y;
        nr = { x: r0.x + dx, y: r0.y + dy, w: r0.w, h: r0.h };
      } else { // resize
        let x1 = r0.x, y1 = r0.y, x2 = r0.x + r0.w, y2 = r0.y + r0.h;
        if (drag.handle.indexOf("w") !== -1) x1 = Math.min(x2 - MIN_CROP, p.x);
        if (drag.handle.indexOf("e") !== -1) x2 = Math.max(x1 + MIN_CROP, p.x);
        if (drag.handle.indexOf("n") !== -1) y1 = Math.min(y2 - MIN_CROP, p.y);
        if (drag.handle.indexOf("s") !== -1) y2 = Math.max(y1 + MIN_CROP, p.y);
        nr = { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
      }
      state.crop = clampRect(nr);
      layoutSel();
    }

    function endDrag(e) {
      if (drag && e && e.target && e.pointerId !== undefined) {
        try { e.target.releasePointerCapture(e.pointerId); } catch (_) {}
      }
      drag = null;
    }

    // Pointer handlers on the canvas (new selection / move existing).
    canvas.addEventListener("pointerdown", function (e) {
      const p = pointToSource(e);
      const c = state.crop;
      const inside = c && p.x >= c.x && p.x <= c.x + c.w
                       && p.y >= c.y && p.y <= c.y + c.h;
      startDrag(e, inside ? "move" : "new", null);
    });
    canvas.addEventListener("pointermove", continueDrag);
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

    // Resize handles need their own listeners so we know which corner.
    sel.querySelectorAll(".ie-handle").forEach(function (h) {
      h.addEventListener("pointerdown", function (e) {
        e.stopPropagation();
        startDrag(e, "resize", h.dataset.h);
      });
      h.addEventListener("pointermove", continueDrag);
      h.addEventListener("pointerup", endDrag);
      h.addEventListener("pointercancel", endDrag);
    });

    // -------------------- toolbar buttons --------------------

    modal.querySelector('[data-act="rotL"]').addEventListener("click", function () {
      if (!state.src) return;
      state.rotation = (state.rotation + 270) % 360;
      state.crop = null;  // crop coords don't map cleanly across rotation
      redrawBase();
    });
    modal.querySelector('[data-act="rotR"]').addEventListener("click", function () {
      if (!state.src) return;
      state.rotation = (state.rotation + 90) % 360;
      state.crop = null;
      redrawBase();
    });
    modal.querySelector('[data-act="reset"]').addEventListener("click", function () {
      if (!state.src) return;
      state.rotation = 0;
      state.crop = null;
      redrawBase();
    });
    modal.querySelector('[data-act="cancel"]').addEventListener("click", function () {
      closeModal(null);
    });
    modal.querySelector('[data-act="apply"]').addEventListener("click", function () {
      if (!state.src) return;
      // Build output canvas: either the cropped region, or the whole rotated frame.
      const out = document.createElement("canvas");
      let sx = 0, sy = 0, sw = canvas.width, sh = canvas.height;
      if (state.crop) {
        sx = Math.round(state.crop.x);
        sy = Math.round(state.crop.y);
        sw = Math.round(state.crop.w);
        sh = Math.round(state.crop.h);
      }
      out.width = sw;
      out.height = sh;
      out.getContext("2d").drawImage(canvas, sx, sy, sw, sh, 0, 0, sw, sh);
      out.toBlob(function (blob) {
        closeModal(blob);
      }, "image/jpeg", 0.92);
    });

    // Choose file… → swap source image.
    const pick = modal.querySelector(".ie-pick");
    modal.querySelector('[data-act="pick"]').addEventListener("click", function () {
      pick.click();
    });
    pick.addEventListener("change", function () {
      const f = pick.files && pick.files[0];
      if (f) loadNewSource(f);
      pick.value = ""; // allow re-picking same file
    });

    if (state.src) redrawBase();
    // Recompute selection position when the canvas display size changes.
    window.addEventListener("resize", layoutSel);
  }

  // -------------------- file → ImageBitmap --------------------

  async function fileToBitmap(file) {
    // Use createImageBitmap with EXIF rotation when possible. Browsers that
    // don't support imageOrientation fall back to plain orientation.
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch (_) {
      try { return await createImageBitmap(file); } catch (_) {}
    }
    // Fallback: load via Image element + canvas.
    return await new Promise(function (resolve, reject) {
      const img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = reject;
      img.src = URL.createObjectURL(file);
    });
  }

  function downscaleIfHuge(bitmap) {
    const w = bitmap.width, h = bitmap.height;
    const longEdge = Math.max(w, h);
    if (longEdge <= MAX_EDGE) return bitmap;
    const s = MAX_EDGE / longEdge;
    const nw = Math.round(w * s), nh = Math.round(h * s);
    const c = document.createElement("canvas");
    c.width = nw; c.height = nh;
    c.getContext("2d").drawImage(bitmap, 0, 0, nw, nh);
    return c;
  }

  // -------------------- pre-upload binding --------------------

  function setupFileInput(input) {
    input.addEventListener("change", async function () {
      const files = Array.from(input.files || []);
      if (!files.length) return;
      const edited = [];
      for (const f of files) {
        let bmp;
        try {
          bmp = await fileToBitmap(f);
        } catch (_) {
          alert("Could not load image: " + f.name);
          input.value = "";
          return;
        }
        const src = downscaleIfHuge(bmp);
        const blob = await openEditor(src);
        if (blob === null) {
          // User cancelled — wipe selection so the form doesn't upload anything.
          input.value = "";
          return;
        }
        edited.push({ orig: f, blob });
      }
      // Substitute the input's files with the edited blobs.
      try {
        const dt = new DataTransfer();
        edited.forEach(function (e) {
          const stem = (e.orig.name || "image").replace(/\.[^.]+$/, "");
          const renamed = new File([e.blob], stem + ".jpg", {
            type: e.blob.type || "image/jpeg",
            lastModified: Date.now(),
          });
          dt.items.add(renamed);
        });
        input.files = dt.files;
      } catch (_) {
        // DataTransfer not supported (very old browsers) — give up silently.
      }
      // Auto-submit the surrounding form so the user doesn't have to press
      // Save themselves after picking + editing the image. Native validation
      // still runs via requestSubmit() — if a required field is empty the
      // browser will block the submit and highlight the offender.
      const form = input.closest("form");
      if (form) {
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      }
    });
  }

  // -------------------- post-upload binding --------------------

  function setupEditButton(btn) {
    btn.addEventListener("click", async function (e) {
      e.preventDefault();
      const src = btn.dataset.editImage || "";
      const post = btn.dataset.editPost;
      if (!post) return;
      const originalLabel = btn.textContent;
      let bmp = null;
      // If the button declares an existing image, load it as the initial
      // source. Otherwise open the editor empty — user picks via Choose file.
      if (src) {
        try {
          const resp = await fetch(src, { cache: "no-store" });
          const fileBlob = await resp.blob();
          bmp = downscaleIfHuge(await fileToBitmap(fileBlob));
        } catch (_) {
          alert("Could not load image for editing.");
          return;
        }
      }
      const blob = await openEditor(bmp);
      if (blob === null) return;
      const fd = new FormData();
      fd.append("image", new File([blob], "edited.jpg", { type: "image/jpeg" }));
      btn.disabled = true; btn.textContent = "Saving…";
      try {
        const r = await fetch(post, { method: "POST", body: fd });
        if (r.ok) { location.reload(); }
        else { alert("Save failed (" + r.status + ")"); btn.disabled = false; btn.textContent = originalLabel; }
      } catch (_) {
        alert("Network error while saving.");
        btn.disabled = false; btn.textContent = originalLabel;
      }
    });
  }

  // -------------------- init --------------------

  function init() {
    document.querySelectorAll("input[type=file][data-edit-on-upload]")
      .forEach(setupFileInput);
    document.querySelectorAll("[data-edit-image][data-edit-post]")
      .forEach(setupEditButton);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
