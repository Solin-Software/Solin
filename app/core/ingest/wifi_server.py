"""
wifi_server.py — Servidor HTTP local para recepção de mídias via Wi-Fi.

Funcionalidades:
  • Sobe um HTTPServer na interface LAN/Wi-Fi (IPv4 local)
  • URL única por sessão com token UUID4 (segurança básica contra varredura)
  • Inatividade ≥ 15 min → para automaticamente (QTimer no main thread)
  • Arquivos recebidos salvos em data/embedded/<uuid><ext>
  • Sinais Qt para atualizar a UI sem bloqueio

Decisões técnicas:
  • O HTTPServer roda em thread daemon pura (stdlib threading).
  • A detecção de inatividade usa _last_activity (float, atomic via GIL)
    e um QTimer de polling no main thread — nenhuma chamada Qt
    é feita diretamente da thread do servidor.
  • Upload multipart parseado manualmente (sem dependências extras):
    lê boundary do Content-Type, itera partes, extrai filename e body.
  • Arquivo salvo atomicamente: write → temp → rename.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote

from PySide6.QtCore import QObject, Signal, QTimer

from app.core.foundation import paths as _paths
from app.core.foundation.constants import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    VIDEO_EXTS,
)

log = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

_INACTIVITY_SECS: int = 15 * 60          # 15 minutos
_POLL_INTERVAL_MS: int = 30_000          # checa inatividade a cada 30 s
_PORT_RANGE: tuple[int, int] = (8765, 8865)
_ALLOWED_EXTS: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS | PDF_EXTS | PLAYLIST_EXTS | JWPUB_EXTS
_MAX_BODY_BYTES: int = 2 * 1024 ** 3    # 2 GB

_MEDIA_TYPE_MAP: dict[str, str] = {}
for _e in VIDEO_EXTS: _MEDIA_TYPE_MAP[_e] = "video"
for _e in AUDIO_EXTS: _MEDIA_TYPE_MAP[_e] = "audio"
for _e in IMAGE_EXTS: _MEDIA_TYPE_MAP[_e] = "image"


# ── Helpers de rede ───────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Retorna o IP da interface LAN/Wi-Fi (não loopback)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _find_free_port(start: int, end: int) -> Optional[int]:
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
                return port
        except OSError:
            continue
    return None


# ── Sanitização de nome de arquivo ────────────────────────────────────────────

def _safe_filename(raw: str) -> str:
    name = re.split(r"[\\/]+", raw)[-1]         # strip caminhos POSIX/Windows
    name = re.sub(r"[^\w\s.\-]", "_", name)    # caracteres seguros
    name = name.strip(". ") or "upload"
    return name[:180]


# ── Parser multipart (sem dependências externas) ──────────────────────────────

def _parse_multipart(body: bytes, boundary: bytes) -> list[dict]:
    """
    Retorna lista de dicts: {"filename": str, "data": bytes, "content_type": str}
    Ignora partes sem filename (campos de formulário comuns).
    """
    delim = b"--" + boundary
    parts: list[dict] = []

    segments = body.split(delim)
    for seg in segments[1:]:                    # primeiro é vazio ou preamble
        if seg.startswith(b"--"):               # epilogue
            break
        # Separa cabeçalhos do corpo (CRLF CRLF)
        try:
            header_end = seg.index(b"\r\n\r\n")
        except ValueError:
            continue
        header_raw = seg[:header_end].decode("utf-8", errors="replace")
        body_part  = seg[header_end + 4:]
        if body_part.endswith(b"\r\n"):
            body_part = body_part[:-2]

        # Extrai filename do Content-Disposition
        cd_match = re.search(
            r'Content-Disposition:[^\r\n]*filename\*?=["\']?(?:utf-8\'\')?([^"\'\r\n;]+)',
            header_raw, re.IGNORECASE,
        )
        if not cd_match:
            continue
        raw_name = cd_match.group(1).strip().strip("\"'")
        raw_name = unquote(raw_name)
        filename = _safe_filename(raw_name)
        if not filename:
            continue

        # Content-Type da parte
        ct_match = re.search(r"Content-Type:\s*(\S+)", header_raw, re.IGNORECASE)
        ct = ct_match.group(1) if ct_match else "application/octet-stream"

        parts.append({"filename": filename, "data": body_part, "content_type": ct})
    return parts


# ── HTML da página de upload ──────────────────────────────────────────────────

def build_upload_html(labels: dict[str, str]) -> str:
    """
    Gera o HTML da página de upload com os textos localizados de `labels`.
    Chaves esperadas: title, subtitle, btn_label, success, error, drop_hint.
    """
    title      = labels.get("title",     "Enviar Mídias")
    subtitle   = labels.get("subtitle",  "Selecione ou arraste fotos, vídeos ou áudios")
    btn_label  = labels.get("btn_label", "Enviar mídias")
    drop_hint  = labels.get("drop_hint", "Arraste arquivos aqui")
    success    = labels.get("success",   "Arquivo recebido!")
    error_lbl  = labels.get("error",     "Erro ao enviar")
    
    # Escapar aspas simples no JS:
    safe_success = success.replace("'", "\\'")
    safe_error_lbl = error_lbl.replace("'", "\\'")

    return (
        "<!DOCTYPE html>"
        '<html lang="pt"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">'
        f"<title>{title}</title>"
        "<style>"
        "*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}"
        ":root{"
        "--bg:#0d1117;--surface:#161b22;--surface2:#1c2128;"
        "--border:#30363d;--border2:#21262d;"
        "--accent:#388bfd;--accent-dk:#1f6feb;"
        "--text:#e6edf3;--muted:#8b949e;"
        "--ok:#3fb950;--err:#f85149;"
        "--r:16px}"
        "html,body{min-height:100%;background:var(--bg);color:var(--text);"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "display:flex;align-items:flex-start;justify-content:center;padding:20px 0 40px}"
        ".card{width:min(94vw,420px);display:flex;flex-direction:column;align-items:center;gap:18px}"
        ".logo-wrap{width:64px;height:64px;background:rgba(56,139,253,.12);"
        "border-radius:20px;display:flex;align-items:center;justify-content:center;margin-top:8px}"
        "h1{font-size:22px;font-weight:700;text-align:center;letter-spacing:-.01em}"
        ".subtitle{font-size:13px;color:var(--muted);text-align:center;line-height:1.55;max-width:300px}"
        ".drop{width:100%;min-height:150px;border:2px dashed var(--border);"
        "border-radius:14px;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;gap:10px;"
        "cursor:pointer;transition:border-color .2s,background .2s;"
        "background:rgba(56,139,253,.03);padding:28px 20px;text-align:center}"
        ".drop.over{border-color:var(--accent);background:rgba(56,139,253,.09)}"
        ".drop svg{opacity:.45}"
        ".drop p{font-size:13px;color:var(--muted)}"
        ".btn{width:100%;padding:20px;font-size:17px;font-weight:700;"
        "border:none;border-radius:14px;cursor:pointer;"
        "background:var(--accent);color:#fff;"
        "transition:background .15s,transform .1s,opacity .2s;"
        "display:flex;align-items:center;justify-content:center;gap:12px;"
        "letter-spacing:.01em;-webkit-tap-highlight-color:transparent}"
        ".btn:hover{background:var(--accent-dk)}"
        ".btn:active{transform:scale(.97)}"
        ".btn:disabled{opacity:.45;cursor:not-allowed;transform:none}"
        ".files{width:100%;display:flex;flex-direction:column;gap:8px}"
        ".fi{display:flex;align-items:center;gap:10px;"
        "background:var(--surface2);border-radius:10px;"
        "border:1px solid var(--border2);padding:11px 13px}"
        ".fi-ico{flex-shrink:0;font-size:20px}"
        ".fi-info{flex:1;min-width:0}"
        ".fi-name{font-size:12px;font-weight:600;white-space:nowrap;"
        "overflow:hidden;text-overflow:ellipsis;color:var(--text)}"
        ".fi-size{font-size:11px;color:var(--muted);margin-top:2px}"
        ".fi-st{flex-shrink:0;font-size:13px;font-weight:700}"
        ".fi-st.wait{color:var(--muted)}"
        ".fi-st.up{color:var(--accent)}"
        ".fi-st.ok{color:var(--ok)}"
        ".fi-st.err{color:var(--err)}"
        ".prog-wrap{width:100%;height:5px;background:var(--border2);"
        "border-radius:3px;overflow:hidden;display:none}"
        ".prog{height:100%;background:var(--accent);border-radius:3px;"
        "transition:width .25s;width:0}"
        ".toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);"
        "background:var(--surface);border:1px solid var(--border);"
        "border-radius:12px;padding:13px 22px;font-size:14px;font-weight:600;"
        "opacity:0;transition:opacity .28s;pointer-events:none;white-space:nowrap;"
        "max-width:88vw;text-align:center}"
        ".toast.show{opacity:1}"
        ".toast.ok{border-color:var(--ok);color:var(--ok)}"
        ".toast.err{border-color:var(--err);color:var(--err)}"
        "input[type=file]{display:none}"
        "</style></head><body>"
        '<div class="card">'
        '<div class="logo-wrap">'
        '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" '
        'stroke="#388bfd" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
        '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>'
        "</svg></div>"
        f"<h1>{title}</h1>"
        f'<p class="subtitle">{subtitle}</p>'
        '<div class="drop" id="dz">'
        '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#8b949e" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="2" y="3" width="20" height="14" rx="2"/>'
        '<path d="M8 21h8M12 17v4"/>'
        '<circle cx="8.5" cy="8.5" r="1.5" fill="#8b949e" stroke="none"/>'
        '<path d="M21 15l-5-5-4 4-2-2-3 3" stroke="#8b949e"/>'
        f"</svg><p>{drop_hint}</p></div>"
        '<div class="files" id="fl"></div>'
        '<div class="prog-wrap" id="pw"><div class="prog" id="pb"></div></div>'
        '<input type="file" id="fi" accept="video/*,audio/*,image/*,.pdf,application/pdf,.jwlplaylist,.jwpub" multiple>'
        f'<button class="btn" id="ub">'
        '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
        '<polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>'
        f"</svg>{btn_label}</button>"
        '</div><div class="toast" id="ts"></div>'
        "<script>"
        "var dz=document.getElementById('dz');"
        "var fi=document.getElementById('fi');"
        "var ub=document.getElementById('ub');"
        "var fl=document.getElementById('fl');"
        "var pw=document.getElementById('pw');"
        "var pb=document.getElementById('pb');"
        "var ts=document.getElementById('ts');"
        "var queue=[];var busy=false;var batchOk=0;var batchErr=0;"
        "function allowed(f){"
        "if(/^(video|audio|image)\\//.test(f.type))return true;"
        "var n=f.name.toLowerCase();"
        "return n.endsWith('.pdf')||n.endsWith('.jwlplaylist')||n.endsWith('.jwpub');}"
        "['dragenter','dragover'].forEach(function(e){"
        "dz.addEventListener(e,function(ev){ev.preventDefault();dz.classList.add('over')});});"
        "['dragleave','drop'].forEach(function(e){"
        "dz.addEventListener(e,function(ev){ev.preventDefault();dz.classList.remove('over')});});"
        "dz.addEventListener('drop',function(ev){"
        "var files=[].slice.call(ev.dataTransfer.files).filter(allowed);"
        "if(files.length)push(files);});"
        "dz.addEventListener('click',function(){fi.click();});"
        "fi.addEventListener('change',function(){"
        "if(fi.files.length)push([].slice.call(fi.files));fi.value='';});"
        "ub.addEventListener('click',function(){fi.click();});"
        "function fs(n){"
        "if(n<1024)return n+' B';"
        "if(n<1048576)return(n/1024).toFixed(1)+' KB';"
        "if(n<1073741824)return(n/1048576).toFixed(1)+' MB';"
        "return(n/1073741824).toFixed(2)+' GB';}"
        "function ico(f){"
        "var n=f.name.toLowerCase();"
        "if(n.endsWith('.pdf'))return'\\uD83D\\uDCC4';"           # 📄
        "if(n.endsWith('.jwlplaylist'))return'\\uD83D\\uDCCB';"   # 📋
        "if(n.endsWith('.jwpub'))return'\\uD83D\\uDCD6';"         # 📖
        "var t=f.type||'';"
        "if(/^video/.test(t))return'\\uD83C\\uDFAC';"   # 🎬
        "if(/^audio/.test(t))return'\\uD83C\\uDFB5';"   # 🎵
        "return'\\uD83D\\uDDBC\\uFE0F';}"               # 🖼️
        "function push(files){"
        "files.forEach(function(f){"
        "var id='f'+Date.now()+Math.random().toString(36).slice(2);"
        "queue.push({f:f,id:id});"
        "var el=document.createElement('div');"
        "el.className='fi';el.id=id;"
        "el.innerHTML='<span class=\"fi-ico\">'+ico(f)+'</span>'"
        "+'<div class=\"fi-info\"><div class=\"fi-name\">'+esc(f.name)+'</div>'"
        "+'<div class=\"fi-size\">'+fs(f.size)+'</div></div>'"
        "+'<span class=\"fi-st wait\" id=\"'+id+'_s\">&#x23F3;</span>';"
        "fl.appendChild(el);});"
        "if(!busy)run();}"
        "function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}"
        "function run(){"
        "if(busy||!queue.length)return;"
        "busy=true;ub.disabled=true;pw.style.display='block';"
        "batchOk=0;batchErr=0;"
        "next();}"
        "function next(){"
        "if(!queue.length){"
        "busy=false;ub.disabled=false;"
        "pb.style.width='100%';"
        f"if(batchOk>0&&batchErr===0){{toast('{safe_success}','ok');}}"
        f"else if(batchErr>0&&batchOk===0){{toast('{safe_error_lbl}','err');}}"
        f"else if(batchOk>0){{toast(batchOk+' \\u2714  '+batchErr+' \\u2716','ok');}}"
        f"else{{toast('{safe_error_lbl}','err');}}"
        "setTimeout(function(){pw.style.display='none';pb.style.width='0';},2200);"
        "return;}"
        "var item=queue.shift();"
        "var st=document.getElementById(item.id+'_s');"
        "if(st){st.className='fi-st up';st.textContent='\\u21E1';}"
        "pb.style.width='0';"
        "upload(item.f,function(ok){"
        "if(ok){batchOk++;}else{batchErr++;}"
        "if(st){st.className='fi-st '+(ok?'ok':'err');"
        "st.textContent=ok?'\\u2714':'\\u2716';}"
        "setTimeout(next,80);});}"
        "function upload(file,cb){"
        "var xhr=new XMLHttpRequest();"
        "xhr.open('POST',window.location.pathname,true);"
        "xhr.upload.onprogress=function(e){"
        "if(e.lengthComputable)pb.style.width=(e.loaded/e.total*100)+'%';};"
        "xhr.onload=function(){cb(xhr.status===200);};"
        "xhr.onerror=function(){cb(false);};"
        "var fd=new FormData();"
        "fd.append('file',file,file.name);"
        "xhr.send(fd);}"
        "function toast(msg,cls){"
        "ts.textContent=msg;ts.className='toast show '+(cls||'');"
        "clearTimeout(ts._t);"
        "ts._t=setTimeout(function(){ts.className='toast';},3500);}"
        "</script></body></html>"
    )


# ── Handler HTTP ──────────────────────────────────────────────────────────────

def _make_handler(token: str, html: str,
                  on_file: Callable[[str, str], None],
                  on_activity: Callable[[], None]) -> type:
    """
    Fábrica que retorna uma classe handler com token/callbacks injetados.
    Usar fábrica em vez de classe global evita estado compartilhado entre sessões.
    """
    class _Handler(BaseHTTPRequestHandler):

        _token      = token
        _html_bytes = html.encode("utf-8")
        _on_file    = staticmethod(on_file)
        _on_act     = staticmethod(on_activity)

        def log_message(self, fmt, *args):  # silencia log do servidor
            pass

        def _send(self, status: int, ct: str, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._on_act()
            path = self.path.split("?")[0].rstrip("/")
            if path == f"/{self._token}":
                self._send(200, "text/html; charset=utf-8", self._html_bytes)
            else:
                self._send(404, "text/plain", b"Not found")

        def do_POST(self):
            self._on_act()
            path = self.path.split("?")[0].rstrip("/")
            if path != f"/{self._token}":
                self._send(404, "text/plain", b"Not found")
                return

            # Lê Content-Length e Content-Type
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                self._send(400, "text/plain", b"Bad request")
                return

            if length > _MAX_BODY_BYTES:
                self._send(413, "text/plain", b"File too large")
                return

            ct = self.headers.get("Content-Type", "")
            bnd_match = re.search(r"boundary=([^\s;]+)", ct)
            if not bnd_match:
                self._send(400, "text/plain", b"No boundary")
                return

            boundary = bnd_match.group(1).strip('"').encode("ascii")

            # Lê corpo completo em buffer (evita ataques de slow-loris com timeout do SO)
            body = self.rfile.read(length)

            parts = _parse_multipart(body, boundary)
            saved = 0
            for part in parts:
                filename = part["filename"]
                ext      = Path(filename).suffix.lower()
                if ext not in _ALLOWED_EXTS:
                    continue
                data = part["data"]
                if not data:
                    continue

                # Salva atomicamente: escreve em .tmp → renomeia
                os.makedirs(_paths.EMBEDDED_DIR, exist_ok=True)
                uid       = uuid.uuid4().hex
                final     = Path(_paths.EMBEDDED_DIR) / f"{uid}{ext}"
                tmp_path  = final.with_suffix(ext + ".tmp")
                try:
                    tmp_path.write_bytes(data)
                    tmp_path.rename(final)
                    self._on_file(str(final), filename)
                    saved += 1
                except OSError:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            if saved:
                self._send(200, "text/plain", b"OK")
            else:
                self._send(422, "text/plain", b"No valid media")

    return _Handler


# ── WifiReceiveServer ─────────────────────────────────────────────────────────

class WifiReceiveServer(QObject):
    """
    Gerencia o ciclo de vida do servidor HTTP de recepção via Wi-Fi.

    Sinais:
      server_started(ip, port, url)   emitido quando o servidor sobe com sucesso
      server_stopped()                emitido quando o servidor para (manual ou inatividade)
      file_received(path, orig_name)  emitido para cada arquivo recebido com sucesso
      error_occurred(message)         emitido quando não é possível iniciar o servidor
      inactivity_stopped()            emitido especificamente quando para por inatividade
    """

    server_started    = Signal(str, int, str)   # ip, port, url
    server_stopped    = Signal()
    file_received     = Signal(str, str)         # saved_path, original_filename
    error_occurred    = Signal(str)
    inactivity_stopped = Signal()
    _shutdown_complete = Signal(int, bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server:        Optional[HTTPServer]      = None
        self._thread:        Optional[threading.Thread] = None
        self._shutdown_thread: Optional[threading.Thread] = None
        self._token:         str                        = ""
        self._last_activity: float                      = 0.0
        self._generation:    int                        = 0
        self._stopping:      bool                       = False
        self._shutdown_inactivity: bool                 = False
        self._pending_start: Optional[dict[str, str]]    = None
        # True enquanto a janela de recepção Wi-Fi estiver visível ao usuário.
        # Quando aberta, o contador de inatividade é suspenso; ao fechar, recomeça
        # do zero (i.e. _last_activity é atualizado no momento em que fecha).
        self._window_open:   bool                       = False

        # QTimer de polling de inatividade (roda no main thread — seguro)
        self._inactivity_timer = QTimer(self)
        self._inactivity_timer.setInterval(_POLL_INTERVAL_MS)
        self._inactivity_timer.timeout.connect(self._check_inactivity)
        self._shutdown_complete.connect(self._finish_shutdown)

    # ── API pública ───────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def token(self) -> str:
        return self._token

    def set_window_visible(self, visible: bool) -> None:
        """
        Informa ao servidor se a janela de recepção Wi-Fi está visível ou não.

        Regras de inatividade:
          • Enquanto visible=True  → o contador NÃO avança (nada vai desligar por inatividade).
          • Ao chamar visible=False → o relógio começa a correr a partir de agora.
          • Ao chamar visible=True  novamente → o relógio é zerado (fresh 15 min).

        Isso garante que o usuário nunca perca o servidor enquanto está olhando para
        a tela, e que sempre tenha 15 minutos completos depois que sair.
        """
        self._window_open = visible
        if not self.is_running:
            return
        if visible:
            # Janela reaberta → zera o contador para dar 15 min completos novamente
            self._last_activity = time.monotonic()
        else:
            # Janela fechada → começa a contar a partir de agora
            self._last_activity = time.monotonic()

    def start(self, html_labels: dict[str, str]) -> bool:
        """
        Inicia o servidor. Retorna True se bem-sucedido.
        Se já estiver rodando, retorna True sem reiniciar.
        """
        if self.is_running:
            return True
        if self._stopping:
            self._pending_start = dict(html_labels)
            return True

        port = _find_free_port(*_PORT_RANGE)
        if port is None:
            self.error_occurred.emit("Nenhuma porta disponível em 8765-8864.")
            return False

        self._token = uuid.uuid4().hex[:12]   # token curto mas suficientemente aleatório
        self._generation += 1
        generation = self._generation
        html = build_upload_html(html_labels)

        handler_cls = _make_handler(
            token       = self._token,
            html        = html,
            on_file=lambda path, name: self._on_file_received(
                generation,
                path,
                name,
            ),
            on_activity=lambda: self._on_activity(generation),
        )

        try:
            server = HTTPServer(("", port), handler_cls)
        except OSError as exc:
            self.error_occurred.emit(str(exc))
            return False

        self._server = server
        self._last_activity = time.monotonic()

        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

        self._inactivity_timer.start()

        ip  = get_local_ip()
        url = f"http://{ip}:{port}/{self._token}"
        self.server_started.emit(ip, port, url)
        return True

    def stop(self, *, wait: bool = False, timeout: float = 5.0) -> None:
        """Para o servidor; server_stopped só é emitido após o término real."""
        self._pending_start = None
        self._begin_shutdown(inactivity=False, wait=wait, timeout=timeout)

    def _begin_shutdown(
        self,
        *,
        inactivity: bool,
        wait: bool,
        timeout: float,
    ) -> None:
        if self._stopping:
            shutdown_thread = self._shutdown_thread
            if wait and shutdown_thread is not None:
                shutdown_thread.join(timeout=max(0.0, timeout))
                if not shutdown_thread.is_alive():
                    self._finish_shutdown(
                        self._generation,
                        self._shutdown_inactivity,
                    )
            return
        if not self.is_running:
            return
        timer = self._inactivity_timer
        if timer.isActive():
            timer.stop()

        server = self._server
        server_thread = self._thread
        generation = self._generation
        self._server = None
        self._token  = ""
        self._stopping = True
        self._shutdown_inactivity = inactivity

        def _shutdown():
            try:
                server.shutdown()
                server.server_close()
                if server_thread is not None and server_thread is not threading.current_thread():
                    server_thread.join(timeout=timeout)
            except Exception:  # noqa: BLE001 - server/thread shutdown boundary
                log.warning("Failed to shutdown Wi-Fi server cleanly", exc_info=True)
            finally:
                try:
                    self._shutdown_complete.emit(generation, inactivity)
                except RuntimeError:
                    return

        shutdown_thread = threading.Thread(
            target=_shutdown,
            daemon=True,
            name=(
                "wifi-server-inactivity-shutdown"
                if inactivity
                else "wifi-server-shutdown"
            ),
        )
        self._shutdown_thread = shutdown_thread
        shutdown_thread.start()
        if wait:
            shutdown_thread.join(timeout=max(0.0, timeout))
            if shutdown_thread.is_alive():
                log.warning("Wi-Fi server did not stop within %.1f seconds", timeout)
            else:
                self._finish_shutdown(generation, inactivity)

    # ── Callbacks do handler (chamados da thread do servidor) ─────────────

    def _on_activity(self, generation: int) -> None:
        """Atualiza timestamp de última atividade (GIL garante atomicidade)."""
        if generation == self._generation and self.is_running:
            self._last_activity = time.monotonic()

    def _on_file_received(self, generation: int, path: str, orig_name: str) -> None:
        """
        Chamado da thread do servidor após salvar o arquivo.
        PySide6 enfileira o sinal automaticamente para o main thread.
        """
        if generation != self._generation or not self.is_running:
            return
        self._last_activity = time.monotonic()
        self.file_received.emit(path, orig_name)

    # ── Polling de inatividade (main thread) ──────────────────────────────

    def _check_inactivity(self) -> None:
        if not self.is_running:
            self._inactivity_timer.stop()
            return
        # Enquanto a janela de recepção estiver aberta, suspende o contador:
        # não deve desligar o servidor com o usuário olhando para a tela.
        if self._window_open:
            self._last_activity = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_activity
        if elapsed >= _INACTIVITY_SECS:
            self._stop_for_inactivity()

    def _stop_for_inactivity(self) -> None:
        self._begin_shutdown(inactivity=True, wait=False, timeout=5.0)

    def _finish_shutdown(self, generation: int, inactivity: bool) -> None:
        if generation != self._generation or not self._stopping:
            return
        self._thread = None
        self._shutdown_thread = None
        self._stopping = False
        self._shutdown_inactivity = False
        self.server_stopped.emit()
        if inactivity:
            self.inactivity_stopped.emit()
        pending, self._pending_start = self._pending_start, None
        if pending is not None:
            self.start(pending)
