from __future__ import annotations

import binascii
import logging
import re
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from PySide6.QtWidgets import QWidget, QApplication
from PySide6.QtCore import Qt, Signal, Slot, QEvent, QTimer
from PySide6.QtGui import QIcon, QPainter, QPen, QColor, QImage

from ...core.foundation.runtime_paths import ProfilePaths
from ...core.i18n.manager import LanguageManager
from ...core.network.browser_settings import normalize_browser_zoom_factor
from ...core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
)
from ...styles.icons import ICON_ASPECT_MATCH, ICON_CAST, ICON_CROP, make_icon
from ...styles.theme import PALETTE
from ...ui.browser.scripts import CURSOR_SPOTLIGHT_JS, CURSOR_SPOTLIGHT_REMOVE_JS
from .crop_overlay import CropOverlay
from .downloads import BrowserDownloadsMixin
from .navigation import BrowserNavigationMixin
from .tab import BrowserTab
from .ui import BrowserUiMixin
from ...ui.incremental_load import IncrementalLoadHandle

log = logging.getLogger(__name__)

#: JPEG quality for the live-tab frame stream (sideview streams JPEG only). Near
#: lossless so the projected page stays clean; the stream is one tab at ~30fps.
_TAB_PROJECTION_JPEG_QUALITY = 95

if TYPE_CHECKING:
    from ...core.media.browser_downloads import BrowserDownloadService
    from ...core.network.browser_images import BrowserImageFetchService
    from ...core.network.browser_settings import BrowserZoomSettings

# ── Overlay JavaScript ─────────────────────────────────────────────────────────
#
# Melhorias v2:
#
#   HOVER CONFIÁVEL
#     • Substituído display:none/block por opacity + pointer-events.
#       display:none remove o elemento do layout e pode disparar mouseleave
#       extras no container; opacity:0 mantém o elemento no fluxo e evita
#       esse efeito colateral.
#     • Cada par (anchor, btn) compartilha um único timer com clearTimeout
#       no mouseenter. Qualquer re-entrada cancela o timer pendente, então
#       sair e voltar rapidamente nunca oculta o botão indevidamente.
#     • O mouseleave do btn agora usa o mesmo timer em vez de esconder
#       imediatamente — mover o cursor do botão de volta ao container não
#       fecha mais o overlay.
#
#   ÍCONES SVG
#     • Botão exibe um ícone SVG inline em vez de texto:
#         – Imagem : monitor com cena de paisagem (montanha + sol)
#         – Vídeo  : monitor com triângulo de play
#     • O texto traduzido vira tooltip (title) e aria-label,
#       mantendo acessibilidade e deixando claro o propósito do botão.
#
#   ESTRATÉGIA DE VÍDEO (inalterada)
#     1. Ao encontrar um <video>, varrer os <source> filhos e armazenar o
#        melhor URL direto (mp4 > webm > outro) antes do VideoJS agir.
#     2. Procurar também em data-* e JSON embutido na página.
#     3. Ao clicar, enviar o URL direto ao bridge Python → QMediaPlayer.
#
import json
from ...styles.icons import JS_SVG_IMAGE, JS_SVG_VIDEO


def _build_overlay_js(body: str) -> str:
    """Injeta as SVG strings vindas de icons.py no corpo do JS overlay."""
    body = re.sub(
        r"// ── Ícones SVG.*?\.join\(''\);",
        (
            f"// ── Ícones SVG (fonte: solin/styles/icons.py) ──────────────────────────\n"
            f"    var SVG_IMAGE = {json.dumps(JS_SVG_IMAGE)};\n"
            f"    var SVG_VIDEO = {json.dumps(JS_SVG_VIDEO)};"
        ),
        body,
        flags=re.DOTALL,
    )
    for old, new in {
        "__SOLIN_CTX_BG__": PALETTE.surface_overlay,
        "__SOLIN_CTX_BORDER__": PALETTE.border,
        "__SOLIN_CTX_SEPARATOR__": PALETTE.border_muted,
        "__SOLIN_CTX_TEXT__": PALETTE.text_secondary,
        "__SOLIN_CTX_MUTED__": PALETTE.text_muted,
        "__SOLIN_CTX_HOVER__": PALETTE.bg3,
    }.items():
        body = body.replace(old, new)
    return body


_OVERLAY_JS_RAW = r"""
(function () {
    'use strict';

    // ── Domínios excluídos: têm tratamento próprio a nível de aplicação ──────
    if (location.hostname === 'stream.jw.org') return;

    var _bridge        = null;
    var _videoSrcMap   = new WeakMap();
    var _imgSaveUrlMap = new WeakMap();  // img/el → original HTTP URL for cache save
    var _bar           = null;  // barra única de botões, filha do <body>
    var _barMedia      = null;  // par {image, video} sob a barra no momento
    var _hideTimer     = null;
    var OVERLAY_BAR_CLASS = '__solin_media_hover_overlay_bar';
    window.__solinMediaHoverOverlaysEnabled =
        window.__solinMediaHoverOverlaysEnabled !== false;

    // ── Ícones SVG overlay ────────────────────────────────────────────────
    // Monitor com paisagem = "projetar imagem"
    var SVG_IMAGE = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"',
        ' viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"',
        ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
        '<rect x="2" y="3" width="20" height="14" rx="2"/>',
        '<line x1="8" y1="21" x2="16" y2="21"/>',
        '<line x1="12" y1="17" x2="12" y2="21"/>',
        '<path d="M6 13 l3-4 2.5 3 2-2.5 3.5 3.5"/>',
        '<circle cx="16.5" cy="7.5" r="1.5" fill="white" stroke="none"/>',
        '</svg>'
    ].join('');

    // Monitor com triângulo de play = "projetar vídeo"
    var SVG_VIDEO = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"',
        ' viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"',
        ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
        '<rect x="2" y="3" width="20" height="14" rx="2"/>',
        '<line x1="8" y1="21" x2="16" y2="21"/>',
        '<line x1="12" y1="17" x2="12" y2="21"/>',
        '<polygon points="10,7.5 10,13.5 16.5,10.5" fill="white" stroke="none"/>',
        '</svg>'
    ].join('');

    // ── Ícones SVG do menu de contexto ────────────────────────────────────
    // Seta de download
    var SVG_CTX_SAVE = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
    // Lista com + (adicionar à playlist)
    var SVG_CTX_PLAYLIST = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="16" y2="18"/><line x1="19" y1="15" x2="19" y2="21"/><line x1="16" y1="18" x2="22" y2="18"/></svg>';

    // ── Sistema de menu de contexto (singleton) ───────────────────────────
    //
    // Apenas um menu aberto por vez. Fecha automaticamente com:
    //   • clique fora (mousedown capture)
    //   • tecla ESC
    //   • scroll na página
    //   • novo contextmenu em outro elemento

    var _ctxMenu     = null;
    var _ctxCleanups = [];

    function _dismissCtx() {
        if (_ctxMenu && _ctxMenu.parentNode) {
            _ctxMenu.parentNode.removeChild(_ctxMenu);
        }
        _ctxMenu = null;
        _ctxCleanups.forEach(function(fn) { try { fn(); } catch(e) {} });
        _ctxCleanups = [];
    }

    function _showCtxMenu(clientX, clientY, items) {
        _dismissCtx();

        var menu = document.createElement('div');
        _ctxMenu = menu;
        menu.setAttribute('role', 'menu');
        menu.style.cssText = [
            'position:fixed',
            'left:' + clientX + 'px',
            'top:' + clientY + 'px',
            'background:__SOLIN_CTX_BG__',
            'border:1px solid __SOLIN_CTX_BORDER__',
            'border-radius:8px',
            'padding:4px 0',
            'z-index:2147483647',
            'min-width:215px',
            'box-shadow:0 8px 32px rgba(0,0,0,0.65),0 2px 8px rgba(0,0,0,0.35)',
            'font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif',
            'user-select:none',
            '-webkit-user-select:none',
        ].join(';');

        items.forEach(function(item) {
            if (item === null) {
                var sep = document.createElement('div');
                sep.style.cssText = 'height:1px;background:__SOLIN_CTX_SEPARATOR__;margin:3px 0;';
                menu.appendChild(sep);
                return;
            }
            var row = document.createElement('div');
            row.setAttribute('role', 'menuitem');
            row.style.cssText = [
                'display:flex', 'align-items:center', 'gap:10px',
                'padding:8px 14px', 'color:__SOLIN_CTX_TEXT__', 'font-size:13px',
                'cursor:pointer', 'line-height:1.2',
            ].join(';');

            var iconEl = document.createElement('span');
            iconEl.innerHTML = item.icon;
            iconEl.style.cssText = 'display:flex;align-items:center;flex-shrink:0;color:__SOLIN_CTX_MUTED__;line-height:0;';

            var labelEl = document.createElement('span');
            labelEl.textContent = item.label;

            row.appendChild(iconEl);
            row.appendChild(labelEl);

            row.addEventListener('mouseenter', function() {
                row.style.background = '__SOLIN_CTX_HOVER__';
                iconEl.style.color = '__SOLIN_CTX_TEXT__';
            });
            row.addEventListener('mouseleave', function() {
                row.style.background = 'transparent';
                iconEl.style.color = '__SOLIN_CTX_MUTED__';
            });
            // preventDefault evita que o browser perca o foco da página
            row.addEventListener('mousedown', function(e) { e.preventDefault(); });
            row.addEventListener('click', function(e) {
                e.preventDefault(); e.stopPropagation();
                _dismissCtx();
                item.action();
            });
            menu.appendChild(row);
        });

        document.body.appendChild(menu);

        // Ajusta posição para não sair da viewport
        var rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth - 8) {
            menu.style.left = Math.max(8, clientX - rect.width) + 'px';
        }
        if (rect.bottom > window.innerHeight - 8) {
            menu.style.top = Math.max(8, clientY - rect.height) + 'px';
        }

        // Registra os dismiss listeners com setTimeout(0) para que o
        // mousedown que ABRIU o menu não feche imediatamente.
        var tmr = setTimeout(function() {
            function onOutside(e) {
                if (_ctxMenu && !_ctxMenu.contains(e.target)) _dismissCtx();
            }
            function onKey(e) { if (e.key === 'Escape') _dismissCtx(); }
            function onScroll() { _dismissCtx(); }

            document.addEventListener('mousedown',   onOutside, true);
            document.addEventListener('keydown',     onKey,     true);
            document.addEventListener('scroll',      onScroll,  { once: true, capture: true });

            _ctxCleanups.push(function() {
                document.removeEventListener('mousedown', onOutside, true);
                document.removeEventListener('keydown',   onKey,     true);
            });
        }, 0);

        _ctxCleanups.push(function() { clearTimeout(tmr); });
    }

    // Monta os items do menu para um tipo de mídia (image|video)
    // Retorna null se não houver URL HTTP válida (não exibir menu)
    function _buildMediaCtxItems(saveUrl, mediaType) {
        if (!saveUrl || !saveUrl.startsWith('http')) return null;

        var saveLabel = mediaType === 'video'
            ? (window._jwSaveVideoLabel   || 'Save video')
            : (window._jwSaveImageLabel   || 'Save image');
        var addLabel  = window._jwAddDestinationLabel || 'Add to…';

        return [
            {
                icon:   SVG_CTX_SAVE,
                label:  saveLabel,
                action: function() {
                    if (_bridge) _bridge.saveMedia(saveUrl, mediaType);
                }
            },
            null, // separador
            {
                icon:   SVG_CTX_PLAYLIST,
                label:  addLabel,
                action: function() {
                    if (!_bridge) return;
                    var title = document.title
                        || saveUrl.split('/').pop().split('?')[0]
                        || '';
                    _bridge.addToDestination(saveUrl, title, mediaType);
                }
            }
        ];
    }

    // ── Canal nativo NativeWebView ─────────────────────────────────────────
    function waitForChannel(attempts) {
        if (attempts <= 0) return;
        if (!window.nativeWebView || !window.nativeWebView.postMessage) {
            setTimeout(function () { waitForChannel(attempts - 1); }, 300);
            return;
        }

        function post(type, payload) {
            payload = payload || {};
            payload.type = type;
            window.nativeWebView.postMessage(payload);
        }

        _bridge = {
            projectImage: function(data) { post('projectImage', { data: data || '' }); },
            projectVideo: function(url) { post('projectVideo', { url: url || '' }); },
            cropSelected: function(x, y, w, h) { post('cropSelected', { x: x, y: y, w: w, h: h }); },
            cancelCrop: function() { post('cancelCrop'); },
            saveMedia: function(url, mediaType) { post('saveMedia', { url: url || '', mediaType: mediaType || '' }); },
            addToDestination: function(url, title, mediaType) {
                post('addToDestination', { url: url || '', title: title || '', mediaType: mediaType || '' });
            }
        };
        window.__solinBridge = _bridge;
        captureMediaUrls();
        observeDOM();
        document.addEventListener('mouseover', onPointerOver, true);
        window.addEventListener('scroll', repositionBar, {capture: true, passive: true});
        window.addEventListener('resize', repositionBar, {passive: true});
    }

    function mediaHoverOverlaysEnabled() {
        return window.__solinMediaHoverOverlaysEnabled !== false;
    }

    function setOverlayBarVisible(bar, visible) {
        var shouldShow = visible && mediaHoverOverlaysEnabled();
        bar.style.opacity = shouldShow ? '1' : '0';
        bar.style.pointerEvents = shouldShow ? 'auto' : 'none';
    }

    function hideAllOverlayBars() {
        document.querySelectorAll('.' + OVERLAY_BAR_CLASS).forEach(function (bar) {
            setOverlayBarVisible(bar, false);
        });
    }

    window.__solinSetMediaHoverOverlaysEnabled = function (enabled) {
        window.__solinMediaHoverOverlaysEnabled = enabled !== false;
        if (!mediaHoverOverlaysEnabled()) hideAllOverlayBars();
    };

    // ── Botão overlay genérico ────────────────────────────────────────────
    function makeBtn(svgContent, handler) {
        var btn = document.createElement('button');
        btn.innerHTML = svgContent;
        btn.style.cssText = [
            'background:rgba(56,139,253,0.90)', 'border:none',
            'border-radius:8px', 'padding:6px', 'cursor:pointer',
            'transition:background 0.15s ease',
            'display:flex', 'align-items:center', 'justify-content:center',
            'box-shadow:0 2px 10px rgba(0,0,0,0.45)', 'line-height:0',
        ].join(';');
        btn.addEventListener('mouseover', function () { btn.style.background = 'rgba(56,139,253,1)'; });
        btn.addEventListener('mouseout',  function () { btn.style.background = 'rgba(56,139,253,0.90)'; });
        btn.addEventListener('click', function (e) {
            e.preventDefault(); e.stopPropagation();
            if (!mediaHoverOverlaysEnabled()) return;
            handler();
        });
        return btn;
    }

    // ── Barra de botões flutuante ─────────────────────────────────────────
    //
    // A barra vive no <body> com position:fixed, nunca dentro da página.
    // Carrosséis e galerias (slick) enumeram os filhos dos seus containers
    // ao inicializar: um nó nosso entre eles virava um slide vazio.
    function ensureBar() {
        if (_bar && _bar.isConnected) return _bar;
        _bar = document.createElement('div');
        _bar.className = OVERLAY_BAR_CLASS;
        _bar.style.cssText = [
            'position:fixed', 'left:0', 'top:0', 'z-index:2147483647',
            'display:flex', 'flex-direction:row', 'gap:6px',
            'opacity:0', 'pointer-events:none',
            'transition:opacity 0.18s ease',
        ].join(';');
        _bar.addEventListener('mouseenter', function () { clearTimeout(_hideTimer); });
        _bar.addEventListener('mouseleave', scheduleHide);
        document.body.appendChild(_bar);
        return _bar;
    }

    // ponytail: só descarta mídia fora da viewport; um container com
    // overflow:hidden ainda pode deixar a barra visível sobre o recorte.
    function placeBar(media) {
        var r = media.getBoundingClientRect();
        if (r.width < 40 || r.height < 40) return false;
        if (r.bottom <= 0 || r.top >= window.innerHeight) return false;
        if (r.right <= 0 || r.left >= window.innerWidth) return false;
        _bar.style.left = Math.round(r.right - 8 - _bar.offsetWidth) + 'px';
        _bar.style.top  = Math.round(r.top + 8) + 'px';
        return true;
    }

    function scheduleHide() {
        clearTimeout(_hideTimer);
        _hideTimer = setTimeout(function () {
            setOverlayBarVisible(ensureBar(), false);
            _barMedia = null;
        }, 120);
    }

    function samePair(a, b) {
        return !!a && !!b && a.image === b.image && a.video === b.video;
    }

    function barAnchor(media) {
        return media.video || media.image;
    }

    function showBarFor(media, buttons) {
        clearTimeout(_hideTimer);
        var bar = ensureBar();
        if (!samePair(_barMedia, media)) {
            bar.innerHTML = '';
            buttons.forEach(function (btn) { bar.appendChild(btn); });
            _barMedia = media;
        }
        setOverlayBarVisible(bar, placeBar(barAnchor(media)));
    }

    function repositionBar() {
        if (!_barMedia || !_bar) return;
        setOverlayBarVisible(_bar, placeBar(barAnchor(_barMedia)));
    }

    // ── Mídia sob o cursor ────────────────────────────────────────────────
    //
    // Resolve o src contra o documento antes do filtro http: páginas como
    // wol.jw.org servem src relativo, que sem isso é rejeitado e derruba o
    // menu de mídia. Retorna '' para data:, blob: e URLs inválidas.
    function absMediaUrl(url) {
        if (!url) return '';
        try {
            var abs = new URL(url, location.href).href;
            return abs.startsWith('http') ? abs : '';
        } catch (e) { return ''; }
    }

    function cssBgUrl(el) {
        if (!el || !el.getAttribute) return '';
        var stored = _imgSaveUrlMap.get(el);
        if (stored) return stored;
        var m = (el.getAttribute('style') || '').match(
            /background-image\s*:\s*url\(\s*['"]?([^'")\s]+)['"]?\s*\)/i
        );
        return m ? absMediaUrl(m[1]) : '';
    }

    function isProjectableImg(el) {
        return el.tagName === 'IMG'
            && (el.naturalWidth  || el.width)  > 80
            && (el.naturalHeight || el.height) > 80;
    }

    // elementsFromPoint alcança o <video> mesmo coberto pelo poster e pelos
    // controles do player, e a <img> sob camadas decorativas da página.
    // Antes do play o poster do VideoJS cobre o vídeo: os dois entram na
    // mesma pilha e cada um recebe o seu botão, como o overlay por âncora fazia.
    function mediaFromPoint(x, y) {
        var stack = document.elementsFromPoint(x, y);
        var image = null, video = null;
        for (var i = 0; i < stack.length; i++) {
            var el = stack[i];
            if (_bar && _bar.contains(el)) continue;
            if (!video && el.tagName === 'VIDEO') video = el;
            else if (!image && (isProjectableImg(el) || cssBgUrl(el))) image = el;
            if (image && video) break;
        }
        return (image || video) ? {image: image, video: video} : null;
    }

    function projectImgElement(img) {
        try {
            var c = document.createElement('canvas');
            c.width  = img.naturalWidth  || img.width;
            c.height = img.naturalHeight || img.height;
            c.getContext('2d').drawImage(img, 0, 0);
            if (_bridge) _bridge.projectImage(c.toDataURL('image/png'));
        } catch (e) {
            if (_bridge) _bridge.projectImage(img.src || img.currentSrc || '');
        }
    }

    function labelledBtn(svg, label, handler) {
        var btn = makeBtn(svg, handler);
        btn.title = label;
        btn.setAttribute('aria-label', label);
        return btn;
    }

    function buttonsFor(media) {
        var buttons = [];
        var image = media.image;
        if (image && image.tagName === 'IMG') {
            buttons.push(labelledBtn(SVG_IMAGE, window._jwProjectLabel || 'Project image',
                function () { projectImgElement(image); }));
        } else if (image) {
            var url = cssBgUrl(image);
            if (url) {
                buttons.push(labelledBtn(SVG_IMAGE, window._jwProjectLabel || 'Project image',
                    function () { if (_bridge) _bridge.projectImage(url); }));
            }
        }
        if (media.video) {
            var video = media.video;
            buttons.push(labelledBtn(SVG_VIDEO, window._jwProjectVideoLabel || 'Project video',
                function () { projectVideoElement(video); }));
        }
        return buttons.length ? buttons : null;
    }

    function onPointerOver(e) {
        if (!mediaHoverOverlaysEnabled()) return;
        if (_bar && _bar.contains(e.target)) { clearTimeout(_hideTimer); return; }
        var media = mediaFromPoint(e.clientX, e.clientY);
        var buttons = media ? buttonsFor(media) : null;
        if (!buttons) { scheduleHide(); return; }
        showBarFor(media, buttons);
    }

    // ── Extração de URL de vídeo ──────────────────────────────────────────
    function bestSrcFromSources(vid) {
        var sources = vid.querySelectorAll('source');
        var mp4 = '', webm = '', any = '';
        for (var i = 0; i < sources.length; i++) {
            var s = sources[i].src || sources[i].getAttribute('src') || '';
            if (!s || s.startsWith('blob:') || !s.startsWith('http')) continue;
            var t = (sources[i].type || '').toLowerCase();
            if (!mp4  && t.indexOf('mp4')  !== -1) { mp4  = s; }
            if (!webm && t.indexOf('webm') !== -1) { webm = s; }
            if (!any) any = s;
        }
        return mp4 || webm || any;
    }

    function srcFromPageJson() {
        var scripts = document.querySelectorAll('script[type="application/json"]');
        for (var i = 0; i < scripts.length; i++) {
            try {
                var found = findMp4InObj(JSON.parse(scripts[i].textContent || ''), 0);
                if (found) return found;
            } catch (e) {}
        }
        return '';
    }

    function findMp4InObj(obj, depth) {
        if (depth > 8 || !obj || typeof obj !== 'object') return '';
        var keys = ['mp4', 'progressiveDownloadURL', 'src', 'url', 'file', 'URI'];
        for (var k = 0; k < keys.length; k++) {
            var v = obj[keys[k]];
            if (typeof v === 'string' && v.startsWith('http') && v.indexOf('.mp4') !== -1) return v;
        }
        var arr = Array.isArray(obj) ? obj : Object.values(obj);
        for (var j = 0; j < arr.length; j++) {
            var r = findMp4InObj(arr[j], depth + 1);
            if (r) return r;
        }
        return '';
    }

    function captureVideoSrc(vid) {
        if (_videoSrcMap.has(vid)) return _videoSrcMap.get(vid);
        var src =
            bestSrcFromSources(vid) ||
            (vid.getAttribute('src') && !vid.getAttribute('src').startsWith('blob:')
                ? vid.getAttribute('src') : '') ||
            vid.getAttribute('data-src') ||
            vid.getAttribute('data-url') || '';
        if (!src) src = srcFromPageJson();
        if (src) _videoSrcMap.set(vid, src);
        return src;
    }

    // ── Projeção de vídeo ─────────────────────────────────────────────────
    function projectVideoElement(vid) {
        var src = _videoSrcMap.get(vid) || captureVideoSrc(vid);
        if (!src) {
            var cs = vid.currentSrc || '';
            if (cs && !cs.startsWith('blob:') && cs.startsWith('http')) src = cs;
        }
        if (src && _bridge) {
            console.log('[Solin] Projecting video direct URL:', src);
            _bridge.projectVideo(src);
        } else {
            console.warn('[Solin] No direct video URL found. currentSrc=', vid.currentSrc);
        }
    }

    // ── Setup geral ───────────────────────────────────────────────────────
    //
    // Guarda a URL original de cada mídia antes que lazy-load ou o player
    // troquem o src. Nada é inserido na página: a barra é criada sob demanda
    // no hover, fora dela.
    function captureMediaUrls() {
        document.querySelectorAll('img').forEach(function (img) {
            if (_imgSaveUrlMap.has(img)) return;
            var url = absMediaUrl((img.getAttribute('src') || img.currentSrc || '').split('?')[0]);
            if (url) _imgSaveUrlMap.set(img, url);
        });
        document.querySelectorAll('video').forEach(captureVideoSrc);
        document.querySelectorAll('[style*="background-image"]').forEach(function (el) {
            if (_imgSaveUrlMap.has(el)) return;
            var url = cssBgUrl(el);
            if (url) _imgSaveUrlMap.set(el, url);
        });
    }

    function earlyCapture(nodes) {
        nodes.forEach(function (n) {
            if (n.nodeType !== 1) return;
            if (n.tagName === 'VIDEO') captureVideoSrc(n);
            n.querySelectorAll && n.querySelectorAll('video').forEach(captureVideoSrc);
        });
    }

    function observeDOM() {
        var obs = new MutationObserver(function (mutations) {
            var added = [];
            mutations.forEach(function (m) { m.addedNodes.forEach(function (n) { added.push(n); }); });
            if (added.length) earlyCapture(added);
            captureMediaUrls();
        });
        obs.observe(document.documentElement, { childList: true, subtree: true });
    }

    // ── Listener delegado de contextmenu (nível de documento) ──────────────
    //
    // Um único listener com capture=true intercepta TODOS os contextmenu.
    // O par {image, video} resolvido por elementsFromPoint é a única fonte
    // da mídia, igual para a barra de hover e para o menu: clicar no player
    // ou no botão do overlay produz o mesmo menu. Sem mídia HTTP válida,
    // retorna sem preventDefault → menu padrão do browser aparece.

    function _addCtxGroup(dest, seen, url, mediaType) {
        if (!url || !url.startsWith('http')) return;
        var key = url + ':' + mediaType;
        if (seen[key]) return;
        seen[key] = true;
        var grp = _buildMediaCtxItems(url, mediaType);
        if (!grp) return;
        if (dest.length > 0) dest.push(null);   // separador entre grupos
        for (var i = 0; i < grp.length; i++) dest.push(grp[i]);
    }

    function imageUrlOf(el) {
        if (el.tagName === 'IMG') {
            return _imgSaveUrlMap.get(el)
                || absMediaUrl((el.getAttribute('src') || el.currentSrc || '').split('?')[0]);
        }
        return cssBgUrl(el);
    }

    function videoUrlOf(vid) {
        var src = _videoSrcMap.get(vid) || captureVideoSrc(vid);
        if (src) return src;
        var current = vid.currentSrc || '';
        return (current && !current.startsWith('blob:')) ? current : '';
    }

    function ctxItemsFor(media) {
        var items = [];
        var seen = {};
        if (media.image) _addCtxGroup(items, seen, imageUrlOf(media.image), 'image');
        if (media.video) _addCtxGroup(items, seen, videoUrlOf(media.video), 'video');
        return items.length > 0 ? items : null;
    }

    // Registrado na fase de capture para agir antes dos demais listeners de contextmenu
    document.addEventListener('contextmenu', function (e) {
        // Sobre o botão do overlay, e.target é o botão: usa o par que a barra cobre.
        var media = (_bar && _bar.contains(e.target) && _barMedia)
            ? _barMedia
            : mediaFromPoint(e.clientX, e.clientY);
        var items = media ? ctxItemsFor(media) : null;
        if (!items) return;               // sem mídia HTTP → menu padrão do browser
        e.preventDefault();
        e.stopPropagation();
        _showCtxMenu(e.clientX, e.clientY, items);
    }, true);

    function earlyVideoScan() {
        document.querySelectorAll('video').forEach(captureVideoSrc);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            earlyVideoScan();
            waitForChannel(40);
        });
    } else {
        earlyVideoScan();
        waitForChannel(40);
    }
})();
"""

# Substitui as definições inline de SVG pelas vindas de icons.py de SVG pelas vindas de icons.py
OVERLAY_JS = _build_overlay_js(_OVERLAY_JS_RAW)

# ── BrowserWidget ──────────────────────────────────────────────────────────────
#
# Layout manual: QTabBar standalone + QPushButton "+" + QStackedWidget.
# O botão "+" é um widget IRMÃO do QTabBar no HBoxLayout — nunca filho.
# Isso elimina para sempre o problema de posicionamento sobre as abas.
#
#  tab_row (QFrame, fundo do tema):
#    QHBoxLayout:
#      [BrowserTabBar — setExpanding(False), scroll buttons]
#      [QPushButton "+"]
#      [stretch — preenche o espaço restante com a cor de fundo]
#
#  QStackedWidget — conteúdo das abas
#  loading_bar

class BrowserWidget(
    BrowserUiMixin,
    BrowserNavigationMixin,
    BrowserDownloadsMixin,
    QWidget,
):
    project_image_signal       = Signal(bytes)
    project_video_signal       = Signal(str, str)
    stop_projection_signal     = Signal()
    project_tab_pixmap_signal  = Signal(object)   # QImage — frame ao vivo da aba pinada
    stop_tab_projection_signal = Signal()
    crop_image_signal          = Signal(bytes)     # PNG bytes da região recortada
    media_destination_signal = Signal(str, str, str, bool)

    _image_fetched_signal = Signal(int, bytes)
    _OVERLAY_JS = OVERLAY_JS

    def __init__(
        self,
        lang_manager: LanguageManager,
        *,
        profile_paths: ProfilePaths,
        zoom_settings: BrowserZoomSettings,
        download_service: BrowserDownloadService,
        image_fetch_service: BrowserImageFetchService,
        playback_protection,
        parent=None,
        projection_fps: int | None = None,
        aspect_ratio_provider: Callable[[], ProjectionAspectRatio] | None = None,
        defer_initial_tabs: bool = False,
    ):
        super().__init__(parent)
        self.lang = lang_manager
        self._download_service = download_service
        self._zoom_settings = zoom_settings
        self._browser_zoom_factor = zoom_settings.zoom_factor()
        self._native_views_occluded = False
        self._aspect_ratio_provider = (
            aspect_ratio_provider or (lambda: DEFAULT_PROJECTION_ASPECT_RATIO)
        )

        self._session_id = profile_paths.native_webview_data_dir.name
        self._session_data_root = profile_paths.native_webview_data_root
        self._image_fetches = image_fetch_service
        self._playback_protection = playback_protection

        self._browser_aspect_locked = False
        self._browser_aspect_ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        self._OVERLAY_JS = _build_overlay_js(_OVERLAY_JS_RAW)

        self._build_ui()
        self._url_bar.set_zoom_factor(self._browser_zoom_factor)
        self.setAcceptDrops(True)
        self._image_fetched_signal.connect(self._deliver_image)

        # ── Estado de projeção de aba ─────────────────────────────────────────
        self._pinned_tab: "BrowserTab | None" = None
        self._tab_projection_fps = self._read_projection_fps(projection_fps)
        self._tab_proj_timer = QTimer(self)
        self._tab_proj_timer.setInterval(self._projection_interval_ms())
        self._tab_proj_timer.timeout.connect(self._grab_pinned_tab)
        # True quando o crop foi iniciado sobre a aba pinada — a projeção fica
        # congelada (timer parado) mas o estado é mantido para poder retomar.
        self._proj_paused_for_crop: bool = False
        self._tab_capture_requests: set[tuple[int, int]] = set()
        self._crop_capture_requests: set[tuple[int, int]] = set()
        self._tab_capture_in_flight: bool = False
        self._tab_frame_stream_active: bool = False

        # ── Spotlight de cursor ───────────────────────────────────────────────
        self._cursor_spotlight_active: bool = False
        # Timer Qt que sincroniza a posição durante drags nativos (scrollbars,
        # etc.) onde o Chromium captura o mouse e para de despachar para o JS.
        self._spotlight_drag_timer = QTimer(self)
        self._spotlight_drag_timer.setInterval(16)   # ~60 fps
        self._spotlight_drag_timer.timeout.connect(self._sync_spotlight_drag_pos)

        self.initial_tabs_preparation = IncrementalLoadHandle(
            (
                lambda: self._new_tab(url=self.lang.wol_url, focus=True),
                lambda: self._new_tab(url="https://www.jw.org", focus=False),
                self._finish_initial_tabs,
            ),
            self,
        )
        if not defer_initial_tabs:
            self.initial_tabs_preparation.complete_now()

        # ── Keyboard shortcuts ────────────────────────────────────────────────
        # QShortcut with WidgetWithChildrenShortcut fires even when Chromium has
        # keyboard focus (unlike keyPressEvent which never fires for NativeWebView).
        from PySide6.QtGui import QKeySequence, QShortcut
        sc_f5 = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        sc_f5.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_f5.activated.connect(self._reload)

        sc_ctrlf5 = QShortcut(QKeySequence("Ctrl+F5"), self)
        sc_ctrlf5.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_ctrlf5.activated.connect(self._clear_cookies_and_reload)

    def _finish_initial_tabs(self) -> None:
        self._tab_bar.setCurrentIndex(0)
        self._update_nav_buttons()

    # ── Projeção ────────────────────────────────────────────────────────────────

    # ── Projeção de aba ao vivo ────────────────────────────────────────────────

    def _read_projection_fps(self, configured: int | None = None) -> int:
        if configured is not None:
            value = int(configured)
        else:
            value = 60
        return max(5, min(60, value))

    def _fallback_projection_fps(self) -> int:
        return min(30, self._tab_projection_fps)

    def _projection_interval_ms(self) -> int:
        return max(1, round(1000 / max(1, self._fallback_projection_fps())))

    def set_tab_projection_fps(self, fps: int) -> None:
        self._tab_projection_fps = max(5, min(60, int(fps)))
        self._tab_proj_timer.setInterval(self._projection_interval_ms())

    def _native_stream_every_nth_frame(self) -> int:
        """Ask WebView2 to skip frames at the producer when the target is lower.

        Chrome DevTools Protocol accepts only integer everyNthFrame values.
        Fractional values like 1.5 are not representable without a custom
        native throttler, so we choose the nearest integer cadence.
        """
        target = max(1, self._tab_projection_fps)
        source_fps = 60
        best_every = 1
        best_delta = abs(source_fps - target)
        for every in range(1, 13):
            effective = source_fps / every
            delta = abs(effective - target)
            if delta < best_delta:
                best_delta = delta
                best_every = every
        return best_every

    def _native_stream_effective_fps(self, every_nth_frame: int) -> float:
        return 60.0 / max(1, every_nth_frame)

    def _activate_tab(self, tab: "BrowserTab") -> None:
        tab.view.ensure_active()
        deferred_url = getattr(tab, "_deferred_url", "")
        if deferred_url:
            tab._deferred_url = ""
            QTimer.singleShot(0, lambda t=tab, u=deferred_url: t.load(u))

    def _on_browser_zoom_factor_changed(
        self,
        source_tab: "BrowserTab",
        factor: float,
    ) -> None:
        factor = normalize_browser_zoom_factor(factor)
        if abs(self._browser_zoom_factor - factor) <= 1e-6:
            return

        self._browser_zoom_factor = factor
        self._url_bar.set_zoom_factor(factor)
        self._zoom_settings.set_zoom_factor(factor)
        for index in range(self._stack.count()):
            tab = self._stack.widget(index)
            if isinstance(tab, BrowserTab) and tab is not source_tab:
                tab.view.set_zoom_factor(factor)

    def set_native_views_occluded(self, occluded: bool) -> None:
        """Temporarily unmap Linux foreign surfaces beneath a Qt overlay."""
        self._native_views_occluded = bool(occluded)
        for index in range(self._stack.count()):
            tab = self._stack.widget(index)
            if isinstance(tab, BrowserTab):
                tab.view.set_native_surface_visible(not self._native_views_occluded)

    def _on_cast_clicked(self, checked: bool):
        tab = self._current_tab()
        if checked and self._pinned_tab is tab:
            self._stop_tab_projection_internal()
            self.stop_tab_projection_signal.emit()
            return
        if checked:
            if not self._playback_protection.allow_manual_projection_change():
                self.cast_btn.blockSignals(True)
                self.cast_btn.setChecked(False)
                self.cast_btn.blockSignals(False)
                self._update_cast_btn_visual(False)
                return
            self._start_tab_projection()
        else:
            self._stop_tab_projection_internal()
            self.stop_tab_projection_signal.emit()

    def _start_tab_projection(self):
        """Pina a aba atual e começa captura no FPS configurado."""
        self._image_fetches.invalidate()
        tab = self._current_tab()
        if not tab:
            # Sem aba válida — desfaz o toggle silenciosamente
            self.cast_btn.blockSignals(True)
            self.cast_btn.setChecked(False)
            self.cast_btn.blockSignals(False)
            self._update_cast_btn_visual(False)
            return
        self._activate_tab(tab)
        self._tab_capture_requests.clear()
        self._tab_capture_in_flight = False
        self._tab_frame_stream_active = False
        self._pinned_tab = tab
        idx = self._tab_bar.currentIndex()
        # Destaque azul no título da aba pinada
        self._tab_bar.setTabTextColor(idx, QColor(PALETTE.accent))
        self._update_cast_btn_visual(True)
        # Mantém o Chromium renderizando mesmo quando BrowserWidget for ocultado
        tab.view.set_projection_active(True)
        if not self._start_native_frame_stream(tab):
            log.info(
                "Tab projection using JPEG snapshot fallback (timer ~%d fps).",
                self._fallback_projection_fps(),
            )
            self._tab_proj_timer.start()

    def _stop_tab_projection_internal(self):
        """Para o timer e limpa estado visual — NÃO emite o sinal externo."""
        self._tab_proj_timer.stop()
        if self._pinned_tab:
            try:
                self._pinned_tab.view.stop_frame_stream()
            except Exception:  # noqa: BLE001 - native webview cleanup boundary
                log.debug("Could not stop pinned tab frame stream", exc_info=True)
        self._tab_capture_requests.clear()
        self._tab_capture_in_flight = False
        self._tab_frame_stream_active = False
        self._proj_paused_for_crop = False
        # Remove destaque de todas as abas
        for i in range(self._tab_bar.count()):
            self._tab_bar.setTabTextColor(i, QColor())
        # Libera o Chromium para pausar renderização normalmente quando oculto
        if self._pinned_tab:
            self._pinned_tab.view.set_projection_active(False)
        self._pinned_tab = None
        self._update_cast_btn_visual(False)
        # Garante que o botão fique desmarcado sem disparar toggled
        self.cast_btn.blockSignals(True)
        self.cast_btn.setChecked(False)
        self.cast_btn.blockSignals(False)

    def _start_native_frame_stream(self, tab: "BrowserTab") -> bool:
        """Prefer native WebView frame streaming; fall back to timer snapshots."""
        every_nth_frame = self._native_stream_every_nth_frame()
        try:
            started = tab.view.start_frame_stream(
                # The stream is JPEG-encoded (sideview has no lossless stream); at
                # q75 the compression is visible when the frame is projected full
                # screen. Use near-lossless quality — the projected page must look
                # clean; the extra bandwidth is fine for a single tab at ~30fps.
                quality=_TAB_PROJECTION_JPEG_QUALITY,
                max_width=max(0, tab.view.width()),
                max_height=max(0, tab.view.height()),
                every_nth_frame=every_nth_frame,
            )
        except Exception as exc:  # noqa: BLE001 - native webview stream boundary
            log.warning("Native frame stream unavailable: %s", exc)
            return False

        self._tab_frame_stream_active = bool(started)
        if started:
            log.info(
                "Tab projection using native WebView frame stream "
                "(everyNthFrame=%d, ~%.1f fps).",
                every_nth_frame,
                self._native_stream_effective_fps(every_nth_frame),
            )
            self._tab_proj_timer.stop()
        else:
            log.info("Native frame stream unavailable on this platform/backend.")
        return bool(started)

    def _resume_tab_projection_after_pause(self):
        if not self._pinned_tab:
            return
        self._tab_capture_in_flight = False
        if not self._start_native_frame_stream(self._pinned_tab):
            log.info(
                "Resuming tab projection with JPEG snapshot fallback (timer ~%d fps).",
                self._fallback_projection_fps(),
            )
            self._tab_proj_timer.start()

    # ── Limpeza de ciclo de vida ────────────────────────────────────────────────

    def cleanup_browser(self) -> None:
        """Compatibility cleanup hook kept for MainWindow shutdown."""
        log.debug("BrowserWidget.cleanup_browser() - disposing native webviews")
        alive_fetches = self._image_fetches.shutdown()
        if alive_fetches:
            log.warning(
                "Browser image fetches still running during shutdown: %s",
                alive_fetches,
            )
        alive_downloads = self._download_service.shutdown()
        if alive_downloads:
            log.warning(
                "Browser downloads still running during shutdown: %s",
                alive_downloads,
            )

        try:
            self._stop_tab_projection_internal()
        except Exception:  # noqa: BLE001 - native webview cleanup boundary
            log.debug("Could not stop tab projection during browser cleanup", exc_info=True)

        for i in range(self._stack.count()):
            w = self._stack.widget(i)
            if not isinstance(w, BrowserTab):
                continue
            try:
                w.view.dispose()
            except Exception as exc:  # noqa: BLE001 - native webview cleanup boundary
                log.debug("cleanup native tab %d: %s", i, exc)

    def _update_cast_btn_visual(self, active: bool):
        if active:
            self.cast_btn.setIcon(
                make_icon(cast(str, ICON_CAST), 16, PALETTE.accent)
            )
            self.cast_btn.setStyleSheet(self._cast_btn_style_on)
            self.cast_btn.setToolTip(self.tr("Stop tab projection"))
        else:
            self.cast_btn.setIcon(
                make_icon(cast(str, ICON_CAST), 16, PALETTE.text_muted)
            )
            self.cast_btn.setStyleSheet(self._cast_btn_style_off)
            self.cast_btn.setToolTip(self.tr("Project this tab live"))

    @staticmethod
    def _make_spotlight_icon(color: str, size: int = 16) -> QIcon:
        """Ícone de círculo-com-ponto para o botão de spotlight."""
        from PySide6.QtGui import QPixmap, QColor
        px = QPixmap(size, size)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(color)
        fill = QColor(c); fill.setAlphaF(0.22)
        ring = QColor(c); ring.setAlphaF(0.65)
        pen = QPen(ring); pen.setWidthF(1.6)
        p.setPen(pen); p.setBrush(fill)
        m = 1.5
        p.drawEllipse(int(m), int(m), int(size - 2*m), int(size - 2*m))
        p.setPen(Qt.PenStyle.NoPen)
        dot = QColor(c); dot.setAlphaF(0.85)
        p.setBrush(dot)
        r = size * 0.16
        cx, cy = size / 2.0, size / 2.0
        p.drawEllipse(int(cx - r), int(cy - r), max(2, int(r*2)), max(2, int(r*2)))
        p.end()
        return QIcon(px)

    # ── Aspect ratio do browser ───────────────────────────────────────────────

    def _on_aspect_lock_toggled(self, checked: bool):
        self._browser_aspect_locked = checked
        if checked:
            self._browser_aspect_ratio = self._resolve_browser_aspect_ratio()
        self._update_aspect_lock_btn_visual(checked)
        for i in range(self._stack.count()):
            tab = self._stack.widget(i)
            if isinstance(tab, BrowserTab):
                self._apply_browser_aspect_to_tab(tab)

    def _apply_browser_aspect_to_tab(self, tab: BrowserTab) -> None:
        tab.set_browser_aspect_ratio_lock(
            self._browser_aspect_locked,
            self._browser_aspect_ratio.value,
        )

    def _update_aspect_lock_btn_visual(self, active: bool):
        if active:
            self.aspect_btn.setIcon(make_icon(ICON_ASPECT_MATCH, 16, PALETTE.accent_alt))
            self.aspect_btn.setStyleSheet(self._aspect_btn_style_on)
            self.aspect_btn.setToolTip(
                self.tr("Return browser to normal size ({ratio} active)").format(
                    ratio=self._browser_aspect_ratio.label,
                )
            )
        else:
            ratio = self._resolve_browser_aspect_ratio()
            self.aspect_btn.setIcon(make_icon(ICON_ASPECT_MATCH, 16, PALETTE.text_muted))
            self.aspect_btn.setStyleSheet(self._aspect_btn_style_off)
            self.aspect_btn.setToolTip(self._aspect_lock_tooltip(ratio))

    def _aspect_lock_tooltip(self, ratio: ProjectionAspectRatio) -> str:
        if ratio.is_fallback:
            return self.tr("Lock browser to 16:9")
        return self.tr("Match browser to projection screen ({ratio})").format(
            ratio=ratio.label,
        )

    def _resolve_browser_aspect_ratio(self) -> ProjectionAspectRatio:
        try:
            ratio = self._aspect_ratio_provider()
        except Exception:  # noqa: BLE001 - defensive projection state boundary
            log.debug("Browser aspect ratio provider failed", exc_info=True)
            return DEFAULT_PROJECTION_ASPECT_RATIO
        if not isinstance(ratio, ProjectionAspectRatio):
            return DEFAULT_PROJECTION_ASPECT_RATIO
        if ratio.width <= 0 or ratio.height <= 0:
            return DEFAULT_PROJECTION_ASPECT_RATIO
        return ratio

    # ── Spotlight de cursor ─────────────────────────────────────────────────────

    def _on_cursor_toggled(self, checked: bool):
        if checked:
            self._activate_cursor_spotlight()
        else:
            self._deactivate_cursor_spotlight()

    def _activate_cursor_spotlight(self):
        self._cursor_spotlight_active = True
        self._update_cursor_btn_visual(True)
        tab = self._current_tab()
        if tab:
            tab.view.page().runJavaScript(self._CURSOR_SPOTLIGHT_JS)
        self._spotlight_drag_timer.start()

    def _deactivate_cursor_spotlight(self):
        self._cursor_spotlight_active = False
        self._spotlight_drag_timer.stop()
        self._update_cursor_btn_visual(False)
        for i in range(self._stack.count()):
            w = self._stack.widget(i)
            if isinstance(w, BrowserTab):
                w.view.page().runJavaScript(self._CURSOR_SPOTLIGHT_REMOVE_JS)

    def _update_cursor_btn_visual(self, active: bool):
        if active:
            self.cursor_btn.setIcon(self._make_spotlight_icon(PALETTE.projection))
            self.cursor_btn.setStyleSheet(self._cursor_btn_style_on)
            self.cursor_btn.setToolTip(self.tr("Disable cursor spotlight"))
        else:
            self.cursor_btn.setIcon(self._make_spotlight_icon(PALETTE.text_muted))
            self.cursor_btn.setStyleSheet(self._cursor_btn_style_off)
            self.cursor_btn.setToolTip(self.tr("Cursor spotlight (presentation mode)"))

    def _sync_spotlight_drag_pos(self):
        """Fallback Qt: sincroniza posição durante drags nativos."""
        from PySide6.QtGui import QCursor

        # Só age quando um botão está pressionado
        if not self._mouse_button_pressed_for_spotlight():
            return

        tab = self._current_tab()
        if not tab:
            return

        view = tab.view

        local = view.mapFromGlobal(QCursor.pos())
        if not view.rect().contains(local):
            return

        view.page().runJavaScript(
            f"if(window.__solinSpotlightSetPos) "
            f"window.__solinSpotlightSetPos({local.x()}, {local.y()});"
        )

    @staticmethod
    def _mouse_button_pressed_for_spotlight() -> bool:
        """Detect mouse state even when WebView2's native HWND captured input."""
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return True

        if sys.platform != "win32":
            return False

        try:
            import ctypes
            user32 = ctypes.windll.user32
            # VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2
            return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04, 0x05, 0x06))
        except Exception:  # noqa: BLE001 - Win32 input-state API boundary
            return False

    def _grab_pinned_tab(self):
        """Captura o frame atual da aba pinada pela API nativa do WebView.

        Nunca usamos captura de tela aqui: qualquer screen-grab capturaria
        toolbar, outros apps ou janelas passando por cima do browser.
        """
        if not self._pinned_tab:
            return

        tab = self._pinned_tab
        tab.view.ensure_active()

        if self._tab_capture_in_flight:
            return
        try:
            request_id = tab.view.capture_frame_jpeg()
        except Exception as exc:  # noqa: BLE001 - native webview capture boundary
            log.warning("Tab capture failed to start: %s", exc)
            self._tab_capture_in_flight = False
            return
        self._tab_capture_in_flight = True
        self._tab_capture_requests.add((id(tab.view), request_id))

    def _on_native_capture_completed(self, tab: "BrowserTab", request_id: int, data: bytes):
        key = (id(tab.view), request_id)
        if key in self._crop_capture_requests:
            self._crop_capture_requests.discard(key)
            self.project_image_signal.emit(data)
            return

        if key in self._tab_capture_requests:
            self._tab_capture_requests.discard(key)
            self._tab_capture_in_flight = False
            image = QImage()
            if image.loadFromData(data) and not image.isNull():
                self.project_tab_pixmap_signal.emit(image)

    def _on_native_capture_failed(self, tab: "BrowserTab", request_id: int, error: str):
        key = (id(tab.view), request_id)
        self._crop_capture_requests.discard(key)
        if key in self._tab_capture_requests:
            self._tab_capture_requests.discard(key)
            self._tab_capture_in_flight = False
        log.warning("Native capture failed: %s", error)

    def _on_native_frame_stream_frame(self, tab: "BrowserTab", data: bytes):
        if tab is not self._pinned_tab or self._proj_paused_for_crop:
            return

        image = QImage()
        if image.loadFromData(data) and not image.isNull():
            self.project_tab_pixmap_signal.emit(image)

    def _on_native_frame_stream_failed(self, tab: "BrowserTab", error: str):
        log.warning("Native frame stream failed: %s", error)
        if tab is self._pinned_tab and not self._proj_paused_for_crop:
            self._tab_frame_stream_active = False
            if not self._tab_proj_timer.isActive():
                log.info(
                    "Falling back to JPEG snapshot projection (timer ~%d fps).",
                    self._fallback_projection_fps(),
                )
                self._tab_proj_timer.start()

    @Slot(str)
    def _on_project_image(self, data: str):
        if not data:
            return
        if data.startswith("data:"):
            import base64
            try:
                _, b64 = data.split(",", 1)
                generation = self._image_fetches.claim()
                if generation is not None:
                    self._deliver_image(generation, base64.b64decode(b64))
            except (ValueError, binascii.Error) as e:
                log.warning("Base64 decode error: %s", e)
        else:
            self._image_fetches.start(
                data,
                self._image_fetched_signal.emit,
            )

    @Slot(int, bytes)
    def _deliver_image(self, generation: int, raw: bytes):
        if self._image_fetches.is_current(generation):
            self.project_image_signal.emit(raw)

    @Slot(str)
    def _on_project_video(self, url: str):
        if not url:
            return
        self._image_fetches.invalidate()
        # Usa o título da aba como fallback; metadados do arquivo sobrescrevem depois
        tab = self._current_tab()
        tab_title = tab.view.title().strip() if tab else ""
        title = tab_title if tab_title else "Video"
        self.project_video_signal.emit(url, title)

    def _do_stop_projection(self):
        self._image_fetches.invalidate()
        self.stop_projection_signal.emit()

    # ── Spotlight de cursor ────────────────────────────────────────────────────
    _CURSOR_SPOTLIGHT_JS = CURSOR_SPOTLIGHT_JS
    _CURSOR_SPOTLIGHT_REMOVE_JS = CURSOR_SPOTLIGHT_REMOVE_JS

    # ── Recorte de região ──────────────────────────────────────────────────────

    def _on_crop_toggled(self, checked: bool):
        if checked:
            self._start_crop_mode()
        else:
            self._cancel_crop_mode()

    def _start_crop_mode(self):
        """Cria o overlay Qt de seleção sobre a aba atual."""
        tab = self._current_tab()
        if not tab:
            self._reset_crop_btn()
            return

        self._activate_tab(tab)

        # Pausa a projeção ao vivo se o crop for na aba pinada
        if self._pinned_tab is not None and self._pinned_tab is tab:
            self._tab_proj_timer.stop()
            if self._tab_frame_stream_active:
                try:
                    tab.view.stop_frame_stream()
                except Exception:  # noqa: BLE001 - native webview cleanup boundary
                    log.debug("Could not pause tab frame stream for crop mode", exc_info=True)
                self._tab_frame_stream_active = False
            self._proj_paused_for_crop = True

        self._update_crop_btn_visual(True)

        # Cria overlay Qt sobre o view — funciona com HTML, PDF ou qualquer conteúdo
        overlay = CropOverlay(tab.view)
        overlay.crop_confirmed.connect(self._on_crop_selected)
        overlay.crop_cancelled.connect(self._on_crop_cancelled)
        overlay.crop_confirmed.connect(lambda *_: overlay.deleteLater())
        overlay.crop_cancelled.connect(overlay.deleteLater)
        self._crop_overlay = overlay
        overlay.setFocus(Qt.FocusReason.OtherFocusReason)

    def _cancel_crop_mode(self):
        """Remove o overlay Qt e cancela o modo de recorte."""
        overlay = getattr(self, '_crop_overlay', None)
        if overlay:
            try:
                overlay.deleteLater()
            except RuntimeError:
                pass
            self._crop_overlay = None
        self._reset_crop_btn()
        if self._proj_paused_for_crop and self._pinned_tab is not None:
            self._proj_paused_for_crop = False
            self._resume_tab_projection_after_pause()

    def _reset_crop_btn(self):
        self.crop_btn.blockSignals(True)
        self.crop_btn.setChecked(False)
        self.crop_btn.blockSignals(False)
        self._update_crop_btn_visual(False)

    def _update_crop_btn_visual(self, active: bool):
        if active:
            self.crop_btn.setIcon(
                make_icon(cast(str, ICON_CROP), 16, PALETTE.warning)
            )
            self.crop_btn.setStyleSheet(self._crop_btn_style_on)
        else:
            self.crop_btn.setIcon(
                make_icon(cast(str, ICON_CROP), 16, PALETTE.text_muted)
            )
            self.crop_btn.setStyleSheet(self._crop_btn_style_off)

    @Slot(float, float, float, float)
    def _on_crop_selected(self, x: float, y: float, w: float, h: float):
        """Captura a região selecionada usando a API nativa do WebView."""
        self._crop_overlay = None
        self._reset_crop_btn()
        self._proj_paused_for_crop = False
        tab = self._current_tab()
        if not tab or w < 4 or h < 4:
            return
        QTimer.singleShot(60, lambda: self._grab_crop_region(tab, x, y, w, h))

    def _grab_crop_region(self, tab, x: float, y: float, w: float, h: float):
        """Captura região selecionada diretamente do WebView nativo."""
        try:
            request_id = tab.view.capture_region(int(x), int(y), int(w), int(h))
        except Exception as exc:  # noqa: BLE001 - native webview capture boundary
            log.warning("Crop capture failed to start: %s", exc)
            return
        self._crop_capture_requests.add((id(tab.view), request_id))

    @Slot()
    def _on_crop_cancelled(self):
        """Usuário cancelou via ESC / clique sem arrastar / blur."""
        self._crop_overlay = None
        self._reset_crop_btn()
        # Se a projeção estava apenas pausada para o crop, retoma normalmente
        if self._proj_paused_for_crop and self._pinned_tab is not None:
            self._proj_paused_for_crop = False
            self._resume_tab_projection_after_pause()

    def _do_stop_projection_real(self):
        self._image_fetches.invalidate()
        self.stop_projection_signal.emit()

    # ── i18n ────────────────────────────────────────────────────────────────────

    def apply_theme(self) -> None:
        self._OVERLAY_JS = _build_overlay_js(_OVERLAY_JS_RAW)
        self._apply_browser_theme_styles()
        for i in range(self._tab_bar.count()):
            tab = self._stack.widget(i)
            self._tab_bar.setTabTextColor(
                i,
                QColor(PALETTE.accent) if tab is self._pinned_tab else QColor(),
            )
        self._update_cast_btn_visual(self.cast_btn.isChecked())
        self._update_crop_btn_visual(self.crop_btn.isChecked())
        self._update_cursor_btn_visual(self._cursor_spotlight_active)
        self._update_aspect_lock_btn_visual(self.aspect_btn.isChecked())
        self.update()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self.back_btn.setToolTip(self.tr("Back (Alt+←)"))
        self.fwd_btn.setToolTip(self.tr("Forward (Alt+→)"))
        self.reload_btn.setToolTip(self.tr("Reload (F5)  ·  Ctrl+F5: clear cookies & reload"))
        self.new_tab_btn.setToolTip(self.tr("New tab (Ctrl+T)"))
        self.url_edit.setPlaceholderText(self.tr("Paste or type a URL…"))
        self.cast_btn.setToolTip(
            self.tr("Stop tab projection") if self.cast_btn.isChecked()
            else self.tr("Project this tab live")
        )
        self.crop_btn.setToolTip(self.tr("Project region of page"))
        self.cursor_btn.setToolTip(
            self.tr("Disable cursor spotlight") if self.cursor_btn.isChecked()
            else self.tr("Cursor spotlight (presentation mode)")
        )
        self._update_aspect_lock_btn_visual(self.aspect_btn.isChecked())

