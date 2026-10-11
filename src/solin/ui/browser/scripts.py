from __future__ import annotations

CURSOR_SPOTLIGHT_JS = r"""
(function () {
    'use strict';
    if (window.__solinCursorSpotlight) return;

    var SIZE = 90;

    // ── Palette ───────────────────────────────────────────────────────────
    var C_DEFAULT = 'rgba(56,189,248,0.38)';
    var C_LEFT    = 'rgba(56,139,253,0.65)';
    var C_RIGHT   = 'rgba(167,139,250,0.58)';
    var S_DEFAULT = '0 0 0 2px rgba(56,189,248,0.28),0 0 26px rgba(56,189,248,0.20)';
    var S_LEFT    = '0 0 0 3px rgba(56,139,253,0.45),0 0 34px rgba(56,139,253,0.38)';
    var S_RIGHT   = '0 0 0 3px rgba(167,139,250,0.45),0 0 32px rgba(167,139,250,0.32)';

    // ── Element ───────────────────────────────────────────────────────────
    var el = document.createElement('div');
    el.id  = '__solin_cursor_spotlight';
    el.style.cssText = [
        'position:fixed',
        'left:-999px','top:-999px',
        'width:' + SIZE + 'px','height:' + SIZE + 'px',
        'border-radius:50%',
        'pointer-events:none',
        'user-select:none','-webkit-user-select:none',
        'z-index:2147483645',
        'transform:translate(-50%,-50%) scale(1)',
        'will-change:transform,left,top,background,box-shadow,opacity',
        'transition:opacity 0.3s ease, background 0.22s ease, box-shadow 0.22s ease',
        'background:'  + C_DEFAULT,
        'box-shadow:'  + S_DEFAULT,
        'border:1.5px solid rgba(255,255,255,0.20)',
        'opacity:0' // The element starts fully invisible.
    ].join(';');
    document.body.appendChild(el);

    // ── State ─────────────────────────────────────────────────────────────
    var curScale   = 1.0;   // Current animated scale (lerp)
    var pressing   = false;
    var isPointer  = false; // The element under the cursor has cursor: pointer.
    var breathPhase = 0;    // Sine phase for the breathing animation
    var firstMove  = true;  // Track the first interaction.

    var SCALE_NORMAL_MIN  = 0.93;
    var SCALE_NORMAL_AMP  = 0.05;   // Sine amplitude around a scale of 1.0
    var SCALE_POINTER     = 1.32;
    var SCALE_PRESS       = 0.80;
    var BREATH_SPEED      = 0.0026; // rad/ms → period ≈ 2.4 s
    var LERP_FACTOR       = 0.14;   // Transition smoothing between states

    // ── rAF loop ──────────────────────────────────────────────────────────
    var prevTs = 0;
    function tick(ts) {
        if (!window.__solinCursorSpotlight) return;
        var dt = prevTs ? Math.min(ts - prevTs, 64) : 16;
        prevTs = ts;

        var targetScale;
        if (pressing) {
            targetScale = SCALE_PRESS;
        } else if (isPointer) {
            targetScale = SCALE_POINTER;
            // Pause breathing while hovering over a pointer target; keep the scale fixed.
        } else {
            breathPhase += BREATH_SPEED * dt;
            targetScale = 1.0 + Math.sin(breathPhase) * SCALE_NORMAL_AMP;
        }

        curScale += (targetScale - curScale) * LERP_FACTOR;
        el.style.transform = 'translate(-50%,-50%) scale(' + curScale.toFixed(4) + ')';

        requestAnimationFrame(tick);
    }

    // ── Tracking ──────────────────────────────────────────────────────────
    // window + capture:true intercepts events before the browser's internal
    // handlers, except for native Chromium widgets such as
    // scrollbars, which are covered by the Qt/Python fallback.

    function onMove(e) {
        el.style.left = e.clientX + 'px';
        el.style.top  = e.clientY + 'px';
        
        if (firstMove) {
            firstMove = false;
            // Reading the element's width forces layout at the new position
            // while opacity is still 0, establishing the transition's initial state.
            void el.offsetWidth; 
            
            // Set opacity to 1 after the layout flush.
            // The CSS transition animates the change.
            el.style.opacity = '1';
        }
        
        // Detect cursor: pointer without calling elementFromPoint: e.target
        // already identifies the element under the cursor, avoiding another hit test.
        var cur = e.target ? window.getComputedStyle(e.target).cursor : 'auto';
        isPointer = (cur === 'pointer') && !pressing;
    }

    function onDown(e) {
        pressing  = true;
        isPointer = false;
        if (e.button === 0) {
            el.style.background = C_LEFT;  el.style.boxShadow = S_LEFT;
        } else if (e.button === 2) {
            el.style.background = C_RIGHT; el.style.boxShadow = S_RIGHT;
        }
    }

    function onUp() {
        pressing = false;
        el.style.background = C_DEFAULT;
        el.style.boxShadow  = S_DEFAULT;
    }

    window.addEventListener('mousemove', onMove, { capture: true, passive: true });
    window.addEventListener('mousedown', onDown, { capture: true, passive: true });
    window.addEventListener('mouseup',   onUp,   { capture: true, passive: true });

    // ── Public API (used by the Qt fallback during native drags) ────────────
    window.__solinSpotlightSetPos = function (x, y) {
        el.style.left = x + 'px';
        el.style.top  = y + 'px';
        if (firstMove) {
            firstMove = false;
            void el.offsetWidth;
            el.style.opacity = '1';
        }
    };

    window.__solinCursorSpotlight = {
        remove: function () {
            window.removeEventListener('mousemove', onMove, { capture: true });
            window.removeEventListener('mousedown', onDown, { capture: true });
            window.removeEventListener('mouseup',   onUp,   { capture: true });
            if (el.parentNode) el.parentNode.removeChild(el);
            window.__solinCursorSpotlight = null;
            window.__solinSpotlightSetPos  = null;
        }
    };

    requestAnimationFrame(tick);
})();
"""

CURSOR_SPOTLIGHT_REMOVE_JS = r"""
(function () {
    if (window.__solinCursorSpotlight) window.__solinCursorSpotlight.remove();
})();
"""
