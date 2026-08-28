"""
translation_editor.py — Solin Translation Editor
─────────────────────────────────────────────────
Editor visual para arquivos .ts do Qt.

Fluxo:
  1. "↻ Extrair strings (lupdate)"  →  cria/atualiza o .ts a partir do código
  2. Traduzir na tabela
  3. "✦ Preencher vazios"           →  auto-tradução via Gemini genai SDK
  4. "💾 Salvar"                     →  grava o .ts
  5. "▶ Compilar .qm (lrelease)"    →  gera o binário para produção

Conceito fundamental:
  • O idioma FONTE (inglês) é o que está escrito em self.tr("...").
    O texto literal é o fallback nativo para mensagens comuns.
  • O catálogo inglês ainda é necessário para mensagens numerus: ele contém
    as formas reais "item"/"items" usadas no lugar do source neutro
    "%n item(s)". Portanto, todos os idiomas disponíveis têm .ts/.qm.

Uso:
    python tools/translation_editor/main.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from typing import Optional

log = logging.getLogger(__name__)

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QRunnable,
    QSortFilterProxyModel, Qt, QThreadPool, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTableView, QTextEdit,
    QVBoxLayout, QWidget,
)
import xml.etree.ElementTree as ET

# ── Paths ──────────────────────────────────────────────────────────────────────
_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
_TRANS_DIR    = os.path.join(
    _PROJECT_ROOT,
    "src",
    "solin",
    "resources",
    "translations",
)
_LANG_DIR     = os.path.join(_TRANS_DIR, "locales")
_GLOSSARY_PATH = os.path.join(_HERE, "jw_glossary.json")

# ── Source language — ordinary strings fall back to their source text ─────────
SOURCE_LANG = "en"

# ── Colours ────────────────────────────────────────────────────────────────────
C = {
    "bg0":"#0d1117","bg1":"#161b22","bg2":"#21262d","bg3":"#30363d",
    "border":"#30363d","accent":"#388bfd","accent_dim":"#1f3a6e",
    "green":"#3fb950","green_dim":"#1a3a24",
    "yellow":"#d29922","yellow_dim":"#3a2f0a",
    "red":"#f85149","orange":"#f0883e",
    "text":"#e6edf3","text_sec":"#8b949e","text_muted":"#484f58",
}

SS = f"""
QMainWindow,QWidget{{background:{C['bg0']};color:{C['text']};
  font-family:'Segoe UI','SF Pro Text',system-ui,sans-serif;font-size:13px;}}
QFrame#TopBar{{background:{C['bg1']};border-bottom:1px solid {C['border']};}}
QFrame#Side{{background:{C['bg1']};border-right:1px solid {C['border']};}}
QPushButton{{background:{C['bg2']};color:{C['text']};border:1px solid {C['border']};
  border-radius:6px;padding:6px 14px;}}
QPushButton:hover{{background:{C['bg3']};border-color:{C['accent']};}}
QPushButton:pressed{{background:{C['accent_dim']};}}
QPushButton:disabled{{color:{C['text_muted']};border-color:{C['bg3']};}}
QPushButton#Compile{{background:{C['accent']};color:white;border:none;font-weight:600;padding:7px 18px;}}
QPushButton#Compile:hover{{background:#58a6ff;}}
QPushButton#Compile:disabled{{background:{C['bg3']};color:{C['text_muted']};}}
QPushButton#Lupdate{{background:{C['yellow_dim']};color:{C['yellow']};border:1px solid {C['yellow']};font-weight:600;}}
QPushButton#Lupdate:hover{{background:{C['yellow']};color:#0d1117;}}
QPushButton#Lupdate:disabled{{background:{C['bg2']};color:{C['text_muted']};border-color:{C['bg3']};}}
QPushButton#Save{{background:{C['green_dim']};color:{C['green']};border:1px solid {C['green']};font-weight:600;}}
QPushButton#Save:hover{{background:{C['green']};color:#0d1117;}}
QPushButton#New{{background:{C['bg2']};color:{C['accent']};border:1px solid {C['accent']};font-weight:600;}}
QPushButton#New:hover{{background:{C['accent_dim']};}}
QPushButton#AutoTranslate{{background:{C['bg2']};color:#a78bfa;border:1px solid #4c1d95;font-weight:600;}}
QPushButton#AutoTranslate:hover{{background:rgba(76,29,149,0.35);border-color:#7c3aed;}}
QPushButton#AutoTranslate:disabled{{color:{C['text_muted']};border-color:{C['bg3']};background:{C['bg2']};}}
QComboBox{{background:{C['bg2']};color:{C['text']};border:1px solid {C['border']};
  border-radius:6px;padding:5px 10px;min-width:160px;}}
QComboBox::drop-down{{border:none;width:24px;}}
QComboBox QAbstractItemView{{background:{C['bg2']};color:{C['text']};
  border:1px solid {C['border']};selection-background-color:{C['accent_dim']};}}
QLineEdit{{background:{C['bg2']};color:{C['text']};border:1px solid {C['border']};
  border-radius:6px;padding:6px 10px;}}
QLineEdit:focus{{border-color:{C['accent']};}}
QTextEdit{{background:{C['bg2']};color:{C['text']};border:1px solid {C['border']};
  border-radius:6px;padding:8px;selection-background-color:{C['accent_dim']};}}
QTextEdit:focus{{border-color:{C['accent']};}}
QTableView{{background:{C['bg0']};color:{C['text']};border:none;
  gridline-color:{C['bg2']};outline:none;selection-background-color:transparent;}}
QTableView::item{{padding:4px 8px;border-bottom:1px solid {C['bg2']};}}
QTableView::item:selected{{background:{C['accent_dim']};color:{C['text']};}}
QHeaderView::section{{background:{C['bg1']};color:{C['text_sec']};border:none;
  border-bottom:1px solid {C['border']};border-right:1px solid {C['border']};
  padding:6px 10px;font-weight:600;font-size:12px;letter-spacing:0.5px;}}
QScrollBar:vertical{{background:{C['bg0']};width:8px;border:none;}}
QScrollBar::handle:vertical{{background:{C['bg3']};border-radius:4px;min-height:30px;}}
QScrollBar::handle:vertical:hover{{background:{C['text_muted']};}}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}
QCheckBox{{color:{C['text_sec']};spacing:6px;}}
QCheckBox::indicator{{width:15px;height:15px;border:1px solid {C['border']};
  border-radius:3px;background:{C['bg2']};}}
QCheckBox::indicator:checked{{background:{C['accent']};border-color:{C['accent']};}}
QSplitter::handle{{background:{C['border']};width:1px;height:1px;}}
QStatusBar{{background:{C['bg1']};color:{C['text_sec']};border-top:1px solid {C['border']};}}
QDialog{{background:{C['bg1']};}}
QFormLayout QLabel{{color:{C['text_sec']};}}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Auto-translate empty .ts entries via Gemini
# ══════════════════════════════════════════════════════════════════════════════


class TranslationGlossaryError(ValueError):
    """Raised when the curated translation glossary cannot be used safely."""


def _load_jw_terminology(path: str = _GLOSSARY_PATH) -> dict[str, dict[str, str]]:
    """Load and validate terminology indexed by the canonical application locale."""

    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise TranslationGlossaryError(f"could not read {path}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TranslationGlossaryError("unsupported or missing glossary schema_version")
    if payload.get("source_locale") != SOURCE_LANG:
        raise TranslationGlossaryError(
            f"glossary source_locale must be {SOURCE_LANG!r}"
        )

    locales = payload.get("locales")
    if not isinstance(locales, dict):
        raise TranslationGlossaryError("glossary locales must be an object")

    validated: dict[str, dict[str, str]] = {}
    for locale, terms in locales.items():
        if not isinstance(locale, str) or not locale.strip() or locale != locale.strip():
            raise TranslationGlossaryError("glossary locale keys must be non-empty strings")
        if not isinstance(terms, dict) or not terms:
            raise TranslationGlossaryError(
                f"glossary locale {locale!r} must contain at least one term"
            )

        validated_terms: dict[str, str] = {}
        for source, translation in terms.items():
            if (
                not isinstance(source, str)
                or not source.strip()
                or not isinstance(translation, str)
                or not translation.strip()
            ):
                raise TranslationGlossaryError(
                    f"glossary locale {locale!r} contains an empty or invalid term"
                )
            validated_terms[source] = translation
        validated[locale] = validated_terms

    return validated



def _build_ts_system_prompt(lang_name: str, lang_code: str) -> str:
    glossary_section = ""
    terms = _load_jw_terminology().get(lang_code)
    if terms:
        term_lines = "\n".join(
            '  "{}" -> "{}"'.format(src, tgt) for src, tgt in terms.items()
        )
        glossary_section = (
            "\n\nOFFICIAL JW GLOSSARY — always use these exact terms for "
            f"{lang_name} (locale: {lang_code}):\n{term_lines}"
        )

    return (
        "You are a professional translator specialised in institutional texts "
        "for Jehovah's Witnesses.\n\n"
        "STYLE RULES:\n"
        "- Use neutral, impersonal, formal language that still reads naturally.\n"
        "- The glossary is authoritative but not exhaustive. For Jehovah's Witnesses-specific concepts not listed there, use the established official terminology found in target-language JW publications; never invent institutional terms.\n"
        "- Perhaps some words can be kept in the translation, such as “Changelog” in Brazilian Portuguese.\n"
        "- Avoid first- and second-person pronouns; prefer impersonal constructions.\n"
        "- Treat personal names used purely as illustrative placeholders (e.g., “John Doe”) as non-specific and adapt or replace them with natural equivalents in the target language when appropriate; however, preserve real person names, brand names, and product names unchanged, unless a widely accepted localized form exists. \n"
        "- Transliterate foreign names or terms only when the target language normally uses another writing system and an official or well-established target-language spelling exists. Never invent a transliteration or transliterate glossary terms, brands, product names, placeholders, URLs, filenames, commands, or identifiers unless authoritative usage explicitly requires it.\n"
        "- The translation must sound native and idiomatic, never word-for-word literal.\n"
        "- Preserve the urgency and tone of the source.\n"
        "- Preserve every placeholder exactly, including repetitions: {variable}, %n, %Ln, %1, and %L1.\n"
        "- Use each entry's context and comments only to disambiguate meaning; do not translate that metadata.\n"
        "- For plural entries, return a JSON array with exactly plural_form_count strings.\n"
        "- For non-plural entries, return a plain string.\n"
        + glossary_section
        + f"\n\nTARGET LANGUAGE: {lang_name} ({lang_code})\n\n"
        "RESPONSE FORMAT:\n"
        "Return ONLY a valid JSON object mapping each opaque entry ID "
        "to its translation (string or array). No markdown, no code fences, no explanation."
    )


def _run_ts_auto_translate(
    entries: list["Entry"],
    lang_name: str,
    lang_code: str,
    api_key: str,
    only_empty: bool = False,
) -> tuple[bool, str, dict]:
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        return False, "google-genai SDK not installed.", {}

    if only_empty:
        empty = [e for e in entries if e.status == "empty"]
    else:
        empty = [e for e in entries if e.status in ("empty", "unfinished")]
    if not empty:
        return True, "No empty entries to translate.", {}

    try:
        system_prompt = _build_ts_system_prompt(lang_name, lang_code)
    except TranslationGlossaryError as exc:
        return False, f"Could not load translation glossary: {exc}", {}

    client = genai.Client(api_key=api_key)

    BATCH_SIZE = 60
    all_results = {}
    total_batches = (len(empty) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(empty), BATCH_SIZE):
        chunk = empty[i : i + BATCH_SIZE]
        batch: dict[str, dict[str, object]] = {}
        
        for e in chunk:
            batch[e.entry_id] = {
                "source": e.source,
                "context": e.context,
                "disambiguation": e.comment,
                "translator_comment": e.extra_comment,
                "is_plural": e.is_plural,
                "plural_form_count": len(e.forms) if e.is_plural else 0,
            }

        user_msg = (
            "Translate each value to {lang} ({code}).\n\n"
            "Source strings (JSON):\n{payload}"
        ).format(
            lang=lang_name,
            code=lang_code,
            payload=json.dumps(batch, ensure_ascii=False, indent=2),
        )

        try:
            response = client.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=user_msg,
                config=gtypes.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2,
                    response_mime_type="application/json",
                    max_output_tokens=81920,
                ),
            )
            raw = (response.text or "").strip()
            chunk_results: dict = json.loads(raw)
            all_results.update(chunk_results)
            
        except json.JSONDecodeError as exc:
            msg = f"Gemini returned invalid JSON on batch {(i//BATCH_SIZE)+1}/{total_batches}: {exc}"
            return False, msg, all_results
        except Exception as exc:  # noqa: BLE001 - external translation service boundary
            msg = f"Translation error on batch {(i//BATCH_SIZE)+1}/{total_batches}: {exc}"
            return False, msg, all_results

    count = len(all_results)
    return True, f"Translated {count} string(s) into {lang_name}.", all_results


# ══════════════════════════════════════════════════════════════════════════════
# Toolchain
# ══════════════════════════════════════════════════════════════════════════════

def _find_tool(name: str) -> list[str]:
    """
    Retorna lista de tokens de comando para a ferramenta PySide6.

    Estratégia:
      1. Wrapper script no PATH (pip normal)
      2. Binário direto dentro do pacote PySide6
         (Anaconda instala lupdate.exe em site-packages/PySide6/)
      3. python -m PySide6.<tool>  (fallback universal)
    Retorna [] se não encontrado.
    """
    import shutil, importlib.util

    short    = name.removeprefix("pyside6-")          # "lupdate" | "lrelease"
    exe_dir  = os.path.dirname(os.path.abspath(sys.executable))

    # 1. wrapper script
    for candidate in [
        name, name + ".exe",
        os.path.join(exe_dir,            name),
        os.path.join(exe_dir,            name + ".exe"),
        os.path.join(exe_dir, "Scripts", name),
        os.path.join(exe_dir, "Scripts", name + ".exe"),
        os.path.join(exe_dir, "bin",     name),
        os.path.join(exe_dir, "bin",     name + ".exe"),
    ]:
        if shutil.which(candidate):
            return [candidate]

    # 2. binário dentro do pacote PySide6 (Anaconda/conda)
    spec = importlib.util.find_spec("PySide6")
    if spec and spec.submodule_search_locations:
        pdir = list(spec.submodule_search_locations)[0]
        for suf in ("", ".exe"):
            full = os.path.join(pdir, short + suf)
            if os.path.isfile(full):
                return [full]

    # 3. módulo Python — funciona em qualquer ambiente
    try:
        importlib.import_module(f"PySide6.{short}")
        return [sys.executable, "-m", f"PySide6.{short}"]
    except ImportError:
        pass

    return []


def _collect_translation_sources(root: str) -> list[str]:
    # Adicionamos "venv_solin" e outros padrões comuns de ignorar
    skip = {"__pycache__", ".git", "build", "dist", "venv", ".venv",
            ".mypy_cache", ".tox", "node_modules", "venv_solin"}
    
    result = []
    for dirpath, dirs, files in os.walk(root):
        # 1. Ignora pastas exatas listadas no skip e qualquer pasta que comece com "venv" ou "env"
        dirs[:] = sorted(
            d for d in dirs 
            if d not in skip and not d.startswith("venv") and not d.startswith("env")
        )
        
        # 2. Segurança extra: ignora se por acaso entrar em um site-packages
        if "site-packages" in dirpath.lower():
            continue
            
        for f in sorted(files):
            if f.startswith("test_"):
                continue
            if f.endswith((".py", ".qml")):
                result.append(os.path.join(dirpath, f))
                
    return result


def run_lupdate(project_root: str, ts_path: str) -> tuple[bool, str]:
    project_root = os.path.abspath(project_root)
    ts_path = os.path.abspath(ts_path)
    cmd_base = _find_tool("pyside6-lupdate")
    if not cmd_base:
        return False, (
            "pyside6-lupdate não encontrado.\n\n"
            f"Python: {sys.executable}\n\n"
            "Instale com:  pip install pyside6"
        )

    source_files = _collect_translation_sources(project_root)
    if not source_files:
        return False, f"Nenhum arquivo .py/.qml encontrado em: {project_root}"

    os.makedirs(os.path.dirname(ts_path), exist_ok=True)
    lst_path: str | None = None

    try:
        # O arquivo de resposta precisa estar fechado no Windows antes de o
        # lupdate abri-lo. Um nome único também permite execuções concorrentes.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".solin-lupdate-",
            suffix=".lst",
            dir=project_root,
            delete=False,
        ) as source_list:
            lst_path = source_list.name
            for source in source_files:
                rel_path = os.path.relpath(source, project_root).replace("\\", "/")
                source_list.write(f"{rel_path}\n")

        cmd = [*cmd_base, f"@{lst_path}", "-ts", ts_path]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
        )
        out = (r.stdout + "\n" + r.stderr).strip()

        if r.returncode != 0 or not os.path.isfile(ts_path):
            executable = " ".join(cmd_base)
            return False, (
                f"Falha no lupdate (código {r.returncode}):\n{out}\n"
                f"Executável: {executable}"
            )

        return True, out

    except subprocess.TimeoutExpired:
        return False, "Timeout (>60s). Tente rodar manualmente no terminal."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Erro executando o comando: {exc}"
    finally:
        if lst_path is not None:
            try:
                os.remove(lst_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("Could not remove lupdate response file %s: %s", lst_path, exc)


def run_lrelease(ts_path: str) -> tuple[bool, str]:
    """Compila .ts → .qm."""
    cmd_base = _find_tool("pyside6-lrelease")
    if not cmd_base:
        return False, (
            "pyside6-lrelease não encontrado.\n\n"
            f"Python: {sys.executable}\n\n"
            "Instale com:  pip install pyside6"
        )
    qm = ts_path.replace(".ts", ".qm")
    try:
        r = subprocess.run(cmd_base + [ts_path, "-qm", qm],
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + "\n" + r.stderr).strip()
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Timeout (>60s)."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Erro: {exc}"


# ── Worker (subprocess em thread separada) ────────────────────────────────────

class _Sig(QObject):
    done = Signal(bool, str)

class _SigAT(QObject):
    """Signal para auto-translate worker (inclui resultados serializados)."""
    done = Signal(bool, str, str)   # (success, message, json_results)

class _Worker(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.signals = _Sig()
        self._fn, self._args = fn, args
    @Slot()
    def run(self):
        self.signals.done.emit(*self._fn(*self._args))

class _AutoTranslateWorker(QRunnable):
    def __init__(self, entries, lang_name, lang_code, api_key,
                 only_empty: bool = False):
        super().__init__()
        self.signals    = _SigAT()
        self._entries   = entries
        self._lang_name = lang_name
        self._lang_code = lang_code
        self._api_key   = api_key
        self._only_empty = only_empty

    @Slot()
    def run(self):
        ok, msg, results = _run_ts_auto_translate(
            self._entries, self._lang_name, self._lang_code,
            self._api_key, self._only_empty,
        )
        self.signals.done.emit(ok, msg, json.dumps(results))


# ══════════════════════════════════════════════════════════════════════════════
# .ts I/O
# ══════════════════════════════════════════════════════════════════════════════

_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}|%L?n|%L?[1-9]\d*")


def _placeholder_counts(text: str) -> Counter[str]:
    return Counter(_PLACEHOLDER_RE.findall(text or ""))


def _placeholder_mismatches(source: str, translations: list[str]) -> list[str]:
    expected = _placeholder_counts(source)
    mismatches: list[str] = []
    for index, translation in enumerate(translations):
        actual = _placeholder_counts(translation)
        if actual == expected:
            continue
        missing = expected - actual
        extra = actual - expected
        details = []
        if missing:
            details.append("missing " + ", ".join(missing.elements()))
        if extra:
            details.append("extra " + ", ".join(extra.elements()))
        label = f"form {index + 1}" if len(translations) > 1 else "translation"
        mismatches.append(f"{label}: {'; '.join(details)}")
    return mismatches


def _validated_translation(entry: "Entry", value: object) -> str | list[str] | None:
    if entry.is_plural:
        if not isinstance(value, list) or len(value) != len(entry.forms):
            return None
        forms = [str(form).strip() for form in value]
        if not forms or any(not form for form in forms):
            return None
        if _placeholder_mismatches(entry.source, forms):
            return None
        return forms
    if not isinstance(value, str) or not value.strip():
        return None
    translation = value.strip()
    if _placeholder_mismatches(entry.source, [translation]):
        return None
    return translation


class Entry:
    """
    Uma <message> do .ts com referência viva ao Element XML.

    Entradas plurais (numerus="yes"):
      • self.is_plural  = True
      • self.forms      = ["forma singular", "forma plural", ...]
      • self.translation = "" (não usado — use forms)

    Entradas simples:
      • self.is_plural  = False
      • self.forms      = []
      • self.translation = "texto traduzido"
    """
    def __init__(
        self,
        elem,
        entry_id: str,
        context: str,
        source: str,
        translation: str,
        finished: bool,
        *,
        comment: str = "",
        extra_comment: str = "",
        location: str = "",
        is_plural: bool = False,
        forms: list[str] | None = None,
    ):
        self._elem        = elem
        self.entry_id     = entry_id
        self.context      = context
        self.source       = source
        self.translation  = translation
        self.finished     = finished
        self.comment      = comment
        self.extra_comment = extra_comment
        self.location     = location
        self.modified     = False
        self.is_plural    = is_plural
        self.forms: list[str] = forms or []

    @property
    def status(self):
        if self.is_plural:
            if not self.forms or all(not f.strip() for f in self.forms):
                return "empty"
            if any(not f.strip() for f in self.forms):
                return "unfinished"
            if not self.finished:
                return "unfinished"
            return "ok"
        if not self.translation.strip():    return "empty"
        if not self.finished:               return "unfinished"
        if self.translation == self.source: return "same"
        return "ok"

    # Para exibição resumida na tabela (coluna Tradução)
    @property
    def display_translation(self) -> str:
        if self.is_plural:
            if not self.forms:
                return ""
            # Mostra a primeira forma preenchida seguida de indicador
            filled = [f for f in self.forms if f]
            if not filled:
                return ""
            count = len(self.forms)
            return f"{filled[0]}  [{count} formas]"
        return self.translation

    @property
    def placeholder_mismatches(self) -> list[str]:
        translations = self.forms if self.is_plural else [self.translation]
        return _placeholder_mismatches(self.source, translations)

    @property
    def details(self) -> str:
        parts = [self.context]
        if self.comment:
            parts.append(f"Disambiguation: {self.comment}")
        if self.extra_comment:
            parts.append(f"Translator note: {self.extra_comment}")
        if self.location:
            parts.append(self.location)
        return "\n".join(part for part in parts if part)


class TsFileSaveError(RuntimeError):
    """The translation file could not be saved atomically."""


class TsFile:
    def __init__(self, path: str):
        self.path  = path
        with open(path, encoding="utf-8", newline="") as stream:
            self._source_text = stream.read()
        self._tree = ET.parse(path)
        self._root = self._tree.getroot()
        self.entries: list[Entry] = []
        self._parse()

    def _parse(self) -> None:
        self.entries.clear()
        for context_index, ctx in enumerate(self._root.iter("context")):
            name_el  = ctx.find("name")
            ctx_name = (name_el.text or "").strip() if name_el is not None else ""

            for message_index, msg in enumerate(ctx.findall("message")):
                src_el = msg.find("source")
                tr_el  = msg.find("translation")
                if src_el is None or tr_el is None:
                    continue
                source = src_el.text or ""

                # Inactive messages stay preserved in XML but are not editable.
                if tr_el.get("type") in {"obsolete", "vanished"}:
                    continue

                finished = tr_el.get("type") != "unfinished"

                # ── Detecta entrada plural ────────────────────────────────────
                is_plural = msg.get("numerus") == "yes"
                forms: list[str] = []
                if is_plural:
                    for nf in tr_el.findall("numerusform"):
                        forms.append(nf.text or "")
                    translation = ""   # não usado para plurais
                else:
                    translation = tr_el.text or ""

                cmt = msg.findtext("comment", default="").strip()
                extra_cmt = msg.findtext("extracomment", default="").strip()
                loc = msg.find("location")
                location = (
                    f"{os.path.basename(loc.get('filename', ''))}"
                    f":{loc.get('line', '')}"
                    if loc is not None
                    else ""
                )

                self.entries.append(Entry(
                    msg,
                    f"{context_index}:{message_index}",
                    ctx_name,
                    source,
                    translation,
                    finished,
                    comment=cmt,
                    extra_comment=extra_cmt,
                    location=location,
                    is_plural=is_plural,
                    forms=forms,
                ))

    @staticmethod
    def _translation_spans(source: str) -> dict[str, tuple[int, int]]:
        """Map stable entry IDs to their lexical ``<translation>`` spans."""
        context_pattern = re.compile(
            r"<context(?:\s[^>]*)?>.*?</context\s*>",
            re.DOTALL,
        )
        message_pattern = re.compile(
            r"<message(?:\s[^>]*)?>.*?</message\s*>",
            re.DOTALL,
        )
        translation_pattern = re.compile(
            r"<translation\b[^>]*(?:/\s*>|>.*?</translation\s*>)",
            re.DOTALL,
        )

        spans: dict[str, tuple[int, int]] = {}
        for context_index, context_match in enumerate(context_pattern.finditer(source)):
            context_text = context_match.group(0)
            for message_index, message_match in enumerate(
                message_pattern.finditer(context_text)
            ):
                message_text = message_match.group(0)
                translation_matches = list(translation_pattern.finditer(message_text))
                if len(translation_matches) != 1:
                    continue
                translation_match = translation_matches[0]
                start = (
                    context_match.start()
                    + message_match.start()
                    + translation_match.start()
                )
                end = (
                    context_match.start()
                    + message_match.start()
                    + translation_match.end()
                )
                spans[f"{context_index}:{message_index}"] = (start, end)
        return spans

    @staticmethod
    def _serialize_translation(entry: Entry) -> str:
        translation = entry._elem.find("translation")
        if translation is None:
            raise ValueError(f"Entry {entry.entry_id} has no translation element")

        original_tail = translation.tail
        try:
            # ElementTree includes an element's tail in tostring(); the tail is
            # outside the replacement span and must remain in the source file.
            translation.tail = None
            return ET.tostring(
                translation,
                encoding="unicode",
                short_empty_elements=False,
            )
        finally:
            translation.tail = original_tail

    def save(self) -> None:
        """
        Persist only modified translations without reformatting the catalog.

        The untouched XML remains byte-for-byte identical. A same-directory
        temporary file keeps the final replacement atomic, and an external
        modification check prevents this editor from overwriting another tool.
        """
        tmp_path = self.path + ".tmp"
        try:
            with open(self.path, encoding="utf-8", newline="") as stream:
                current_text = stream.read()
            if current_text != self._source_text:
                raise RuntimeError(
                    "the translation file changed outside this editor"
                )

            modified = [entry for entry in self.entries if entry.modified]
            if not modified:
                return

            spans = self._translation_spans(self._source_text)
            replacements: list[tuple[int, int, str]] = []
            for entry in modified:
                span = spans.get(entry.entry_id)
                if span is None:
                    raise RuntimeError(
                        f"could not locate translation entry {entry.entry_id}"
                    )
                replacements.append((*span, self._serialize_translation(entry)))

            serialized = self._source_text
            for start, end, replacement in sorted(replacements, reverse=True):
                serialized = serialized[:start] + replacement + serialized[end:]

            with open(tmp_path, "w", encoding="utf-8", newline="") as stream:
                stream.write(serialized)
            os.replace(tmp_path, self.path)
            self._source_text = serialized
        except Exception as exc:  # noqa: BLE001 - atomic-save rollback boundary
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                log.warning("Could not remove temporary translation file %s", tmp_path, exc_info=True)
            raise TsFileSaveError(f"Could not save translation file {self.path}") from exc


# ══════════════════════════════════════════════════════════════════════════════
# Detecção de idiomas
# ══════════════════════════════════════════════════════════════════════════════

def load_languages() -> list[dict]:
    """
    Retorna todos os idiomas definidos em translations/locales/*.json.
    Campos: code, name, is_source, ts (path|None), qm (path|None).
    """
    import glob
    langs: dict[str, dict] = {}

    for jpath in sorted(glob.glob(os.path.join(_LANG_DIR, "*.json"))):
        try:
            with open(jpath, encoding="utf-8") as f:
                d = json.load(f)
            meta = d["meta"]
            code = meta["code"]
            langs[code] = {
                "code":      code,
                "name":      meta.get("name", code),
                "api_code":  meta.get("api_code", "E"),
                "is_source": code == SOURCE_LANG,
                "json_path": jpath,
                "ts":        None,
                "qm":        None,
            }
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            log.debug("Could not read locale metadata from %s", jpath, exc_info=True)

    for code, info in langs.items():
        ts = os.path.join(_TRANS_DIR, f"solin_{code}.ts")
        qm = os.path.join(_TRANS_DIR, f"solin_{code}.qm")
        if os.path.isfile(ts): info["ts"] = ts
        if os.path.isfile(qm): info["qm"] = qm

    return list(langs.values())


def save_lang_json(code: str, name: str,
                   api_code: str, wol_lang: str, wol_region: str,
                   wol_lp: str, date_format: str,
                   time_with_seconds_format: str) -> str:
    """Cria translations/locales/<code>.json e retorna o path."""
    os.makedirs(_LANG_DIR, exist_ok=True)
    path = os.path.join(_LANG_DIR, f"{code}.json")
    data = {"meta": {
        "code": code, "name": name,
        "api_code": api_code,
        "wol_lang": wol_lang, "wol_region": wol_region, "wol_lp": wol_lp,
        "date_format": date_format,
        "time_with_seconds_format": time_with_seconds_format,
    }}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# Modelo de tabela
# ══════════════════════════════════════════════════════════════════════════════

_SL = {"ok":"✓  OK","same":"≈  Igual","unfinished":"○  Pendente","empty":"!  Vazia"}
_SC = {"ok":C["green"],"same":C["yellow"],"unfinished":C["red"],"empty":C["orange"]}


class Model(QAbstractTableModel):
    HEADERS = ["Status", "Contexto", "Inglês (source)", "Tradução"]
    COL_ST, COL_CTX, COL_SRC, COL_TR = 0, 1, 2, 3

    dirty_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[Entry] = []
        self._dirty = False

    def load(self, rows: list[Entry]):
        self.beginResetModel()
        self._rows  = rows
        self._dirty = False
        self.endResetModel()

    def rowCount(self, _=None):    return len(self._rows)
    def columnCount(self, _=None): return 4

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[s]

    def data(self, idx: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not idx.isValid(): return None
        e, col = self._rows[idx.row()], idx.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return [_SL.get(e.status, e.status), e.context, e.source, e.display_translation][col]

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == self.COL_ST:  return QColor(_SC.get(e.status, C["text_sec"]))
            if col == self.COL_CTX: return QColor(C["text_muted"])
            if e.modified:          return QColor(C["accent"])
            if e.placeholder_mismatches: return QColor(C["orange"])

        if role == Qt.ItemDataRole.BackgroundRole:
            if e.modified:     return QColor(C["accent_dim"])

        if role == Qt.ItemDataRole.ToolTipRole:
            if e.placeholder_mismatches:
                return "⚠ Placeholder mismatch: " + " | ".join(
                    e.placeholder_mismatches
                )

        if role == Qt.ItemDataRole.UserRole:
            return e

    def flags(self, _):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def update_entry(self, row: int, value: "str | list[str]"):
        """
        Atualiza a tradução de uma entrada.
        Para entradas simples: value é str.
        Para entradas plurais: value é list[str] com uma string por forma.
        """
        e = self._rows[row]
        if e.is_plural:
            new_forms = value if isinstance(value, list) else [value]
            if len(new_forms) != len(e.forms):
                raise ValueError(
                    "Plural form count cannot change while editing a TS entry"
                )
            if new_forms == e.forms:
                return
            e.forms    = new_forms
            e.finished = bool(new_forms) and all(f.strip() for f in new_forms)
            e.modified = True
            tr_el = e._elem.find("translation")
            if tr_el is not None:
                # Atualiza cada <numerusform> individualmente
                existing = tr_el.findall("numerusform")
                for i, form_text in enumerate(new_forms):
                    existing[i].text = form_text or None
                if e.finished:
                    tr_el.attrib.pop("type", None)
                else:
                    tr_el.set("type", "unfinished")
        else:
            translation = value if isinstance(value, str) else (value[0] if value else "")
            if translation == e.translation:
                return
            e.translation = translation
            e.finished    = bool(translation)
            e.modified    = True
            tr_el = e._elem.find("translation")
            if tr_el is not None:
                tr_el.text = translation
                if e.finished:
                    tr_el.attrib.pop("type", None)
                else:
                    tr_el.set("type", "unfinished")
        self._mark_dirty(True)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.COL_TR))
    def _mark_dirty(self, v: bool) -> None:
        if v != self._dirty:
            self._dirty = v
            self.dirty_changed.emit(v)

    def mark_clean(self) -> None:
        """Marca o modelo como sem alterações pendentes (chamar após save).
        Também limpa o flag `modified` de todas as entradas para que o filtro
        'Só alterados' fique vazio depois de salvar — comportamento esperado.
        """
        for e in self._rows:
            e.modified = False
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, self.COL_TR),
            )
        self._mark_dirty(False)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def stats(self):
        ok   = sum(1 for e in self._rows if e.status == "ok")
        pend = sum(1 for e in self._rows if e.status in ("unfinished","empty"))
        same = sum(1 for e in self._rows if e.status == "same")
        warn = sum(1 for e in self._rows if e.placeholder_mismatches)
        return {"ok":ok,"pending":pend,"same":same,"warnings":warn,"total":len(self._rows)}


# ══════════════════════════════════════════════════════════════════════════════
# Proxy de filtro
# ══════════════════════════════════════════════════════════════════════════════

class Proxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pend     = False
        self._modified = False
        self._txt      = ""
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_text(self, t):
        self._txt = t.lower(); self.invalidateFilter()

    def set_pending(self, v):
        self._pend = v; self.invalidateFilter()

    def set_modified(self, v):
        self._modified = v; self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        e: Entry = self.sourceModel().data(
            self.sourceModel().index(row, 0), Qt.ItemDataRole.UserRole)
        if e is None: return True
        if self._pend     and e.status not in ("unfinished", "empty"): return False
        if self._modified and not e.modified:                           return False
        if self._txt:
            haystack = " ".join(
                [
                    e.context,
                    e.comment,
                    e.extra_comment,
                    e.location,
                    e.source,
                    e.translation,
                    *e.forms,
                ]
            )
            return self._txt in haystack.lower()
        return True


# ══════════════════════════════════════════════════════════════════════════════
# Painel de detalhe
# ══════════════════════════════════════════════════════════════════════════════

# ── Rótulos de forma plural por idioma ───────────────────────────────────────
# Muitos idiomas têm 2 formas (singular/plural). Alguns (russo, árabe, polonês)
# têm 3+. Usamos os rótulos genéricos como fallback seguro.
_PLURAL_LABELS: dict[int, list[str]] = {
    1: ["Singular"],
    2: ["Singular (1)", "Plural (n ≠ 1)"],
    3: ["Forma 1 (1)", "Forma 2 (2–4)", "Forma 3 (n=0, 5+)"],
    4: ["Forma 1", "Forma 2", "Forma 3", "Forma 4"],
    6: ["Zero", "Um", "Dois", "Poucos", "Muitos", "Outro"],  # árabe
}

def _plural_labels(n: int) -> list[str]:
    if n in _PLURAL_LABELS:
        return _PLURAL_LABELS[n]
    return [f"Forma {i+1}" for i in range(n)]


class DetailPanel(QFrame):
    """
    Painel lateral de edição.

    Modo simples (entry.is_plural=False):
      Exibe um único QTextEdit para a tradução.

    Modo plural (entry.is_plural=True):
      Exibe N QTextEdits — um por <numerusform> — com rótulos
      (Singular, Plural, …) derivados da quantidade de formas no arquivo.
      Qualquer edição em qualquer campo habilita o botão Salvar.
    """
    committed = Signal(int, object)   # (row, str | list[str])

    def __init__(self, parent=None):
        super().__init__(parent)
        self._row: Optional[int] = None
        self._loading = False
        self._is_plural = False
        self._plural_edits: list[QTextEdit] = []   # edits ativos no modo plural
        self._build()

    # ── construção base (widgets permanentes) ─────────────────────────────

    def _build(self):
        self.setObjectName("Side")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(16, 16, 16, 16)
        self._lay.setSpacing(10)

        # Contexto
        self._ctx = QLabel("—")
        self._ctx.setStyleSheet(
            f"color:{C['text_muted']};font-size:11px;font-family:monospace;")
        self._ctx.setWordWrap(True)
        self._lay.addWidget(self._ctx)

        # Source
        self._lay.addWidget(self._section("INGLÊS (source)"))
        self._src = QTextEdit()
        self._src.setReadOnly(True)
        self._src.setFixedHeight(85)
        self._src.setStyleSheet(
            f"QTextEdit{{background:{C['bg0']};color:{C['text_sec']};"
            f"border:1px solid {C['bg3']};border-radius:6px;padding:8px;}}")
        self._lay.addWidget(self._src)

        # ── Área de tradução (simples ou plural) ─────────────────────────
        # Usamos um QWidget container para poder trocar os widgets internos
        # sem reconstruir o layout inteiro.
        self._tr_container = QWidget()
        self._tr_container.setObjectName("tr_container")
        self._tr_layout = QVBoxLayout(self._tr_container)
        self._tr_layout.setContentsMargins(0, 0, 0, 0)
        self._tr_layout.setSpacing(6)
        self._lay.addWidget(self._tr_container)

        # Tradução simples (default — sempre presente, ocultado no modo plural)
        self._tr_label = self._section("TRADUÇÃO")
        self._tr_layout.addWidget(self._tr_label)
        self._tr = QTextEdit()
        self._tr.setFixedHeight(105)
        self._tr.textChanged.connect(self._changed)
        self._tr_layout.addWidget(self._tr)

        # Copy source button
        self._copy_btn = QPushButton("⬇  Copiar source")
        self._copy_btn.setFixedHeight(28)
        self._copy_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:1px solid {C['bg3']};"
            f"color:{C['text_muted']};font-size:11px;border-radius:5px;}}"
            f"QPushButton:hover{{border-color:{C['accent']};color:{C['accent']};}}")
        self._copy_btn.clicked.connect(self._do_copy_source)
        self._lay.addWidget(self._copy_btn)

        # Warnings
        self._warn = QLabel("")
        self._warn.setStyleSheet(f"color:{C['orange']};font-size:11px;")
        self._warn.setWordWrap(True)
        self._lay.addWidget(self._warn)

        # Save
        self._save_btn = QPushButton("✓  Salvar  (Ctrl+Enter)")
        self._save_btn.setObjectName("Save")
        self._save_btn.setMinimumHeight(38)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._commit)
        self._lay.addWidget(self._save_btn)
        self._lay.addStretch()
        QShortcut(QKeySequence("Ctrl+Return"), self, self._commit)

    # ── helpers de UI ─────────────────────────────────────────────────────

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{C['text_sec']};font-size:11px;font-weight:600;"
            f"letter-spacing:0.5px;")
        return lbl

    def _make_plural_edit(self, label: str, height: int = 62) -> tuple[QLabel, QTextEdit]:
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color:{C['text_muted']};font-size:11px;font-weight:600;"
            f"letter-spacing:0.3px;padding-top:4px;")
        ed = QTextEdit()
        ed.setFixedHeight(height)
        ed.textChanged.connect(self._changed)
        return lbl, ed

    def _clear_plural_widgets(self):
        """Remove todos os widgets de formas plurais do container."""
        for ed in self._plural_edits:
            # Remove do layout e destrói
            self._tr_layout.removeWidget(ed)
            ed.setParent(None)
            ed.deleteLater()
        self._plural_edits.clear()
        # Remove também os labels das formas (ficam antes de cada edit)
        while self._tr_layout.count() > 0:
            item = self._tr_layout.itemAt(self._tr_layout.count() - 1)
            if item and item.widget():
                w = item.widget()
                if w not in (self._tr_label, self._tr):
                    self._tr_layout.removeWidget(w)
                    w.setParent(None)
                    w.deleteLater()
                else:
                    break
            else:
                break

    def _show_simple_mode(self):
        """Mostra o QTextEdit simples, esconde formas plurais."""
        self._clear_plural_widgets()
        self._tr_label.setText("TRADUÇÃO")
        self._tr_label.show()
        self._tr.show()
        self._copy_btn.show()

    def _show_plural_mode(self, forms: list[str]):
        """
        Esconde o QTextEdit simples e cria um QTextEdit por forma plural.
        As formas são: [singular_text, plural_text, ...].
        """
        # Oculta widgets simples
        self._tr_label.hide()
        self._tr.hide()
        self._copy_btn.hide()
        self._clear_plural_widgets()

        # Cabeçalho do bloco plural
        header = self._section(f"TRADUÇÃO  ({len(forms)} formas plurais)")
        self._tr_layout.addWidget(header)
        self._plural_edits_labels: list[QLabel] = []

        labels = _plural_labels(len(forms))
        for _i, (lbl_text, form_text) in enumerate(zip(labels, forms, strict=False)):
            lbl, ed = self._make_plural_edit(lbl_text.upper())
            self._loading = True
            ed.setPlainText(form_text)
            self._loading = False
            self._tr_layout.addWidget(lbl)
            self._tr_layout.addWidget(ed)
            self._plural_edits.append(ed)

    # ── API pública ────────────────────────────────────────────────────────

    def load(self, row: int, entry: "Entry"):
        self._row, self._loading = row, True
        self._is_plural = entry.is_plural

        self._ctx.setText(entry.details or "—")
        self._src.setPlainText(entry.source)

        if entry.is_plural:
            self._show_plural_mode(entry.forms)
            self._copy_btn.hide()
        else:
            self._show_simple_mode()
            self._tr.setPlainText(entry.translation)

        self._save_btn.setEnabled(False)
        self._update_warn(entry)
        self._loading = False

    def clear(self):
        self._row, self._loading = None, True
        self._is_plural = False
        self._ctx.setText("—")
        self._src.clear()
        self._show_simple_mode()
        self._tr.clear()
        self._save_btn.setEnabled(False)
        self._warn.setText("")
        self._loading = False

    # ── internos ──────────────────────────────────────────────────────────

    def _update_warn(self, entry: "Entry"):
        mv = entry.placeholder_mismatches
        self._warn.setText(
            f"⚠ Placeholders inconsistentes: {' | '.join(mv)}" if mv else "")

    def _do_copy_source(self):
        """Copia o source para o campo de tradução simples."""
        self._tr.setPlainText(self._src.toPlainText())

    def _changed(self):
        if not self._loading:
            self._save_btn.setEnabled(True)

    def _commit(self):
        if self._row is None:
            return
        if self._is_plural:
            if not self._plural_edits:
                return
            # Coleta todas as formas
            value: list[str] = [ed.toPlainText() for ed in self._plural_edits]
        else:
            value: str = self._tr.toPlainText()
        self.committed.emit(self._row, value)
        self._save_btn.setEnabled(False)

# ══════════════════════════════════════════════════════════════════════════════
# Diálogo "Novo idioma"
# ══════════════════════════════════════════════════════════════════════════════

class NewLangDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Novo idioma")
        self.setFixedWidth(420)
        self.setModal(True)

        lay = QVBoxLayout(self)
        lay.setSpacing(16)

        info = QLabel(
            "Preencha os metadados do novo idioma.\n"
            "Um arquivo translations/locales/<code>.json será criado automaticamente.\n"
            "Depois clique em '↻ Extrair strings' para gerar o .ts.")
        info.setStyleSheet(f"color:{C['text_sec']};font-size:12px;")
        info.setWordWrap(True)
        lay.addWidget(info)

        form = QFormLayout()
        form.setSpacing(10)

        def field(placeholder="", tooltip=""):
            e = QLineEdit(); e.setPlaceholderText(placeholder)
            if tooltip: e.setToolTip(tooltip)
            return e

        self._code    = field("pt_BR",       "Código BCP-47 (ex: pt_BR, ja, es)")
        self._name    = field("Português",   "Nome exibido no seletor de idioma")
        self._api     = field("T",            "Código da API JW (ex: T para Português)")
        self._wol_l   = field("pt",           "Língua no WOL (ex: pt, en, es)")
        self._wol_r   = field("r5",           "Região WOL (ex: r5, r1)")
        self._wol_lp  = field("lp-t",        "lp-code WOL (ex: lp-t, lp-e)")
        self._datefmt = field("dd/MM/yyyy HH:mm", "Formato de data Qt")
        self._timefmt = field("HH:mm:ss", "Formato de hora Qt com segundos")

        for label, widget in [
            ("Código *",       self._code),
            ("Nome *",         self._name),
            ("Código API",     self._api),
            ("WOL língua",     self._wol_l),
            ("WOL região",     self._wol_r),
            ("WOL lp",         self._wol_lp),
            ("Formato data",   self._datefmt),
            ("Formato hora",   self._timefmt),
        ]:
            form.addRow(label, widget)

        lay.addLayout(form)

        self._err = QLabel("")
        self._err.setStyleSheet(f"color:{C['red']};font-size:12px;")
        lay.addWidget(self._err)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _accept(self):
        code = self._code.text().strip()
        name = self._name.text().strip()
        if not code:
            self._err.setText("O código do idioma é obrigatório."); return
        if not re.match(r'^[a-zA-Z]{2,8}(_[a-zA-Z]{2,8})?$', code):
            self._err.setText("Código inválido. Use formato BCP-47: pt_BR, ja, es…"); return
        if not name:
            self._err.setText("O nome é obrigatório."); return
        if os.path.isfile(os.path.join(_LANG_DIR, f"{code}.json")):
            self._err.setText(f"Idioma '{code}' já existe."); return
        self.accept()

    def values(self) -> dict:
        return {
            "code": self._code.text().strip(),
            "name": self._name.text().strip(),
            "api_code": self._api.text().strip() or "E",
            "wol_lang": self._wol_l.text().strip() or "en",
            "wol_region": self._wol_r.text().strip() or "r1",
            "wol_lp": self._wol_lp.text().strip() or "lp-e",
            "date_format": self._datefmt.text().strip() or "MM/dd/yyyy HH:mm",
            "time_with_seconds_format": self._timefmt.text().strip() or "HH:mm:ss",
        }


# ══════════════════════════════════════════════════════════════════════════════
# Diálogo de output de processo
# ══════════════════════════════════════════════════════════════════════════════

class OutputDialog(QDialog):
    def __init__(self, title: str, output: str, success: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(660, 380)
        lay = QVBoxLayout(self)

        badge = QLabel("✓  Concluído" if success else "✗  Falhou")
        badge.setStyleSheet(
            f"color:{'#3fb950' if success else '#f85149'};"
            f"font-weight:700;font-size:14px;padding:4px 0;")
        lay.addWidget(badge)

        out = QTextEdit()
        out.setReadOnly(True)
        out.setPlainText(output or "(sem saída)")
        out.setStyleSheet(
            f"QTextEdit{{background:{C['bg0']};color:{C['text_sec']};"
            f"border:1px solid {C['bg3']};border-radius:6px;"
            f"font-family:monospace;font-size:12px;padding:8px;}}")
        lay.addWidget(out)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


# ══════════════════════════════════════════════════════════════════════════════
# Janela principal
# ══════════════════════════════════════════════════════════════════════════════

class TranslationEditor(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Solin — Translation Editor")
        self.resize(1340, 800)
        self.setMinimumSize(960, 580)

        self._langs: list[dict]         = load_languages()
        self._current: Optional[dict]   = None
        self._tsfile: Optional[TsFile]  = None
        self._model  = Model()
        self._proxy  = Proxy()
        self._proxy.setSourceModel(self._model)
        self._pool   = QThreadPool.globalInstance()
        self._busy_operation: str | None = None
        self._auto_translate_target: tuple[str, str, int] | None = None
        self._auto_translate_entry_ids: set[str] = set()

        self._build_ui()
        self._model.dirty_changed.connect(self._on_dirty)

        self._refresh_combo(select_code=None)

    # ── Build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        w = QWidget(); self.setCentralWidget(w)
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0,0,0,0); vl.setSpacing(0)
        vl.addWidget(self._mk_topbar())

        sp = QSplitter(Qt.Orientation.Horizontal)
        sp.setChildrenCollapsible(False)

        left = QWidget(); ll = QVBoxLayout(left)
        ll.setContentsMargins(0,0,0,0); ll.setSpacing(0)
        ll.addWidget(self._mk_filterbar())
        ll.addWidget(self._mk_table())
        sp.addWidget(left)

        self._detail = DetailPanel()
        self._detail.setFixedWidth(350)
        self._detail.committed.connect(self._on_committed)
        sp.addWidget(self._detail)
        sp.setStretchFactor(0,1); sp.setStretchFactor(1,0)
        vl.addWidget(sp, 1)

        sb = self.statusBar()
        self._status = QLabel("Pronto")
        self._status.setContentsMargins(8,0,0,0)
        sb.addWidget(self._status)
        self._progress = QLabel("")
        self._progress.setContentsMargins(0,0,12,0)
        sb.addPermanentWidget(self._progress)

    def _mk_topbar(self) -> QFrame:
        bar = QFrame(); bar.setObjectName("TopBar"); bar.setFixedHeight(58)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20,0,20,0); lay.setSpacing(10)

        ttl = QLabel("⚙  Translation Editor")
        ttl.setStyleSheet(f"font-size:15px;font-weight:700;color:{C['text']};")
        lay.addWidget(ttl); lay.addSpacing(12)

        lay.addWidget(QLabel("Idioma:"))
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_lang_changed)
        lay.addWidget(self._combo)

        # Novo idioma
        self._btn_new = QPushButton("＋  Novo idioma")
        self._btn_new.setObjectName("New")
        self._btn_new.setMinimumHeight(34)
        self._btn_new.clicked.connect(self._do_new_lang)
        lay.addWidget(self._btn_new)

        lay.addStretch()

        # lupdate
        self._btn_lu = QPushButton("↻  Extrair strings  (lupdate)")
        self._btn_lu.setObjectName("Lupdate")
        self._btn_lu.setMinimumHeight(36)
        self._btn_lu.setToolTip(
            "Escaneia o código Python e atualiza o .ts.\n"
            "Strings existentes são preservadas.")
        self._btn_lu.clicked.connect(self._do_lupdate)
        lay.addWidget(self._btn_lu)

        # auto translate
        self._btn_auto_tr = QPushButton("✦  Preencher vazios")
        self._btn_auto_tr.setObjectName("AutoTranslate")
        self._btn_auto_tr.setMinimumHeight(36)
        self._btn_auto_tr.setToolTip(
            "Auto-translate (Gemini)\n"
            "Preenche todas as strings vazias/pendentes para o idioma selecionado.\n"
            "Nada é salvo até você clicar em 💾 Salvar."
        )
        self._btn_auto_tr.setEnabled(False)
        self._btn_auto_tr.clicked.connect(self._do_auto_translate)
        lay.addWidget(self._btn_auto_tr)

        # save
        self._btn_sv = QPushButton("💾  Salvar  (Ctrl+S)")
        self._btn_sv.setObjectName("Save")
        self._btn_sv.setMinimumHeight(36)
        self._btn_sv.setEnabled(False)
        self._btn_sv.clicked.connect(self._do_save)
        QShortcut(QKeySequence("Ctrl+S"), self, self._do_save)
        lay.addWidget(self._btn_sv)

        # lrelease
        self._btn_lr = QPushButton("▶  Compilar .qm  (lrelease)")
        self._btn_lr.setObjectName("Compile")
        self._btn_lr.setMinimumHeight(36)
        self._btn_lr.setToolTip(
            "Compila o .ts em .qm binário para produção.\n"
            "Somente o .qm é necessário no app — nunca o .ts.")
        self._btn_lr.clicked.connect(self._do_lrelease)
        lay.addWidget(self._btn_lr)

        return bar

    def _mk_filterbar(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background:{C['bg1']};border-bottom:1px solid {C['border']};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12,8,12,8); lay.setSpacing(10)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar por contexto, texto ou tradução…")
        self._search.textChanged.connect(self._proxy.set_text)
        self._search.setMinimumWidth(280)
        lay.addWidget(self._search, 1)

        # ── Filtro: só pendentes ──────────────────────────────────────────
        self._chk = QCheckBox("Só pendentes")
        self._chk.toggled.connect(self._on_filter_pending)
        lay.addWidget(self._chk)

        # ── Filtro: só alterados (não salvo) ─────────────────────────────
        self._chk_mod = QCheckBox("Só alterados")
        self._chk_mod.setToolTip(
            "Mostra apenas as entradas modificadas desde o último salvamento.\n"
            "Útil para revisar tudo antes de pressionar Ctrl+S.")
        self._chk_mod.setStyleSheet(
            f"QCheckBox{{color:{C['accent']};spacing:6px;}}"
            f"QCheckBox::indicator{{width:15px;height:15px;border:1px solid {C['border']};"
            f"border-radius:3px;background:{C['bg2']};}}"
            f"QCheckBox::indicator:checked{{background:{C['accent']};border-color:{C['accent']};}}"
        )
        self._chk_mod.toggled.connect(self._on_filter_modified)
        lay.addWidget(self._chk_mod)

        self._stats_lbl = QLabel("")
        self._stats_lbl.setStyleSheet(f"color:{C['text_sec']};font-size:12px;")
        lay.addWidget(self._stats_lbl)
        return w

    def _on_filter_pending(self, checked: bool):
        """Ativa filtro 'pendentes'; desativa 'alterados' se necessário."""
        if checked and self._chk_mod.isChecked():
            self._chk_mod.blockSignals(True)
            self._chk_mod.setChecked(False)
            self._chk_mod.blockSignals(False)
            self._proxy.set_modified(False)
        self._proxy.set_pending(checked)

    def _on_filter_modified(self, checked: bool):
        """Ativa filtro 'alterados'; desativa 'pendentes' se necessário."""
        if checked and self._chk.isChecked():
            self._chk.blockSignals(True)
            self._chk.setChecked(False)
            self._chk.blockSignals(False)
            self._proxy.set_pending(False)
        self._proxy.set_modified(checked)

    def _mk_table(self) -> QTableView:
        t = QTableView(); t.setModel(self._proxy)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().hide(); t.setShowGrid(False)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed);  t.setColumnWidth(0,100)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed);  t.setColumnWidth(1,200)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        t.verticalHeader().setDefaultSectionSize(32)
        t.selectionModel().currentRowChanged.connect(self._on_row)
        # Atalhos de navegação
        QShortcut(QKeySequence("j"), t,
                  lambda: self._move_row(1))
        QShortcut(QKeySequence("k"), t,
                  lambda: self._move_row(-1))
        self._table = t
        return t

    # ── Combo ──────────────────────────────────────────────────────────────

    def _refresh_combo(self, select_code: Optional[str] = None):
        """Reconstrói o combo a partir de self._langs."""
        self._combo.blockSignals(True)
        self._combo.clear()
        for lg in self._langs:
            badge = "  (fonte)" if lg["is_source"] else ""
            ts_ok = "  ✓" if lg["ts"] else ""
            self._combo.addItem(f"{lg['name']}  ({lg['code']}){badge}{ts_ok}")
        self._combo.blockSignals(False)

        # Seleciona idioma desejado
        idx = 0
        if select_code:
            idx = next((i for i,lg in enumerate(self._langs)
                        if lg["code"] == select_code), 0)
        self._combo.setCurrentIndex(idx)
        if self._langs:
            self._load_lang(self._langs[idx])

    # ── Dados ──────────────────────────────────────────────────────────────

    def _load_lang(self, lang: dict):
        """Carrega idioma. Trata idioma fonte e .ts ausente."""
        self._current = lang
        self._tsfile = None
        self._model.load([])
        self._detail.clear()
        self._update_stats()

        self._btn_lu.setEnabled(True)
        self._btn_lr.setEnabled(True)
        self._btn_auto_tr.setEnabled(False)

        ts = lang.get("ts")
        if not ts or not os.path.isfile(ts):
            self._set_status(
                f"solin_{lang['code']}.ts não existe — "
                f"clique em '↻ Extrair strings' para criá-lo.")
            return

        try:
            tsfile = TsFile(ts)
        except (OSError, ET.ParseError, ValueError) as exc:
            self._set_status(f"Erro ao abrir {ts}: {exc}", error=True)
            return

        self._tsfile = tsfile
        self._model.load(self._tsfile.entries)
        # Força atualização completa do proxy e da view
        self._proxy.invalidateFilter()
        self._table.reset()
        self._detail.clear()
        self._update_stats()
        
        has_empty = any(e.status in ("empty", "unfinished") for e in self._tsfile.entries)
        self._btn_auto_tr.setEnabled(has_empty and not lang["is_source"])

        n = len(self._tsfile.entries)
        self._set_status(f"Carregado: {os.path.basename(ts)}  ({n} strings)")

    def _on_lang_changed(self, idx: int):
        if idx < 0 or idx >= len(self._langs): return
        if self._busy_operation:
            return
        if self._model.dirty:
            r = QMessageBox.question(
                self, "Salvar?",
                "Traduções não salvas. Salvar antes de trocar de idioma?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                # Reverte combo
                self._combo.blockSignals(True)
                old = next((i for i,lg in enumerate(self._langs)
                            if lg is self._current), 0)
                self._combo.setCurrentIndex(old)
                self._combo.blockSignals(False)
                return
            if r == QMessageBox.StandardButton.Save:
                if not self._do_save():
                    self._combo.blockSignals(True)
                    old = next((i for i, lg in enumerate(self._langs)
                                if lg is self._current), 0)
                    self._combo.setCurrentIndex(old)
                    self._combo.blockSignals(False)
                    return
        self._load_lang(self._langs[idx])

    def _on_row(self, current: QModelIndex, _prev):
        if not current.isValid():
            self._detail.clear(); return
        src = self._proxy.mapToSource(current)
        e = self._model.data(self._model.index(src.row(),0), Qt.ItemDataRole.UserRole)
        if e: self._detail.load(src.row(), e)

    def _on_committed(self, row: int, value):
        self._model.update_entry(row, value)
        self._update_stats()
        
        has_empty = any(e.status in ("empty", "unfinished") for e in self._tsfile.entries)
        self._btn_auto_tr.setEnabled(has_empty and self._current and not self._current["is_source"])

    def _on_dirty(self, dirty: bool):
        self._btn_sv.setEnabled(dirty)
        t = "Solin — Translation Editor"
        self.setWindowTitle(t + ("  •  modificado" if dirty else ""))

    def _move_row(self, delta: int):
        sm  = self._table.selectionModel()
        cur = sm.currentIndex()
        new = self._proxy.index(
            max(0, min(self._proxy.rowCount()-1, cur.row()+delta)), 0)
        sm.setCurrentIndex(new, sm.SelectionFlag.ClearAndSelect |
                                sm.SelectionFlag.Rows)
        self._table.scrollTo(new)

    # ── Novo idioma ────────────────────────────────────────────────────────

    def _do_new_lang(self):
        dlg = NewLangDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        try:
            save_lang_json(**v)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            QMessageBox.critical(self, "Erro", f"Não foi possível criar o JSON:\n{exc}")
            return

        # Recarrega lista e seleciona novo idioma
        self._langs = load_languages()
        self._refresh_combo(select_code=v["code"])
        self._set_status(
            f"Idioma '{v['name']}' criado. "
            f"Clique em '↻ Extrair strings' para gerar o .ts.")

    # ── Toolchain ──────────────────────────────────────────────────────────

    def _do_lupdate(self):
        if not self._current or self._busy_operation:
            return

        ts_path = (self._current.get("ts") or
                   os.path.join(_TRANS_DIR, f"solin_{self._current['code']}.ts"))

        if self._model.dirty:
            r = QMessageBox.question(
                self, "Salvar antes?",
                "Salvar traduções antes de rodar o lupdate?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel: return
            if r == QMessageBox.StandardButton.Save and not self._do_save():
                return

        self._set_busy(True, "lu")
        w = _Worker(run_lupdate, _PROJECT_ROOT, ts_path)
        self._pending_ts_path = ts_path
        w.signals.done.connect(self._lu_done)
        self._pool.start(w)

    @Slot(bool, str)
    def _lu_done(self, ok: bool, output: str):
        ts_path = getattr(self, "_pending_ts_path", "")
        self._set_busy(False, "lu")

        OutputDialog("lupdate — " + ("OK" if ok else "Falhou"),
                     output, ok, self).exec()

        if ok:
            # Atualiza a referência em ambos os lugares de forma consistente
            for lg in self._langs:
                if lg is self._current:
                    lg["ts"] = ts_path
                    break
            self._current["ts"] = ts_path
            self._load_lang(self._current)
            # Atualiza badge no combo
            idx  = self._combo.currentIndex()
            self._combo.setItemText(
                idx,
                f"{self._current['name']}  ({self._current['code']})  ✓")

    def _do_save(self) -> bool:
        if not self._tsfile:
            return False
        try:
            self._tsfile.save()
            self._model.mark_clean()
            self._set_status(f"✓  Salvo: {os.path.basename(self._tsfile.path)}")
            return True
        except TsFileSaveError as exc:
            log.error("Could not save translation file", exc_info=True)
            self._set_status(f"Erro ao salvar: {exc}", error=True)
            return False

    def _do_lrelease(self):
        if not self._current or not self._current.get("ts"):
            QMessageBox.warning(self, "Sem .ts",
                "Rode o lupdate primeiro para gerar o .ts.")
            return
        if self._busy_operation:
            return
        if self._model.dirty and not self._do_save():
            return
        self._set_busy(True, "lr")
        w = _Worker(run_lrelease, self._current["ts"])
        w.signals.done.connect(self._lr_done)
        self._pool.start(w)

    @Slot(bool, str)
    def _lr_done(self, ok: bool, output: str):
        self._set_busy(False, "lr")
        OutputDialog("lrelease — " + ("OK" if ok else "Falhou"),
                     output, ok, self).exec()
        if ok and self._current:
            qm = self._current["ts"].replace(".ts", ".qm")
            self._current["qm"] = qm
            kb = os.path.getsize(qm) // 1024 if os.path.isfile(qm) else 0
            self._set_status(f"✓  Compilado: {os.path.basename(qm)}  ({kb} KB)")

    def _do_auto_translate(self):
        if not self._current or not self._tsfile or self._busy_operation:
            return

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            QMessageBox.warning(
                self, "Sem chave API",
                "Defina a variável de ambiente GEMINI_API_KEY para usar o preenchimento automático."
            )
            return

        empty_count = sum(
            1 for e in self._tsfile.entries if e.status in ("empty", "unfinished")
        )
        only_empty_count = sum(
            1 for e in self._tsfile.entries if e.status == "empty"
        )
        if empty_count == 0:
            self._set_status("Nenhuma string vazia para traduzir.")
            return

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Preencher vazios")
        msg_box.setText(
            f"Como deseja traduzir as strings pendentes de {self._current['name']}?\n\n"
            f"• <b>Traduzir tudo</b>: {empty_count} string(s) — inclui vazias e não-finalizadas\n"
            f"• <b>Apenas vazios</b>: {only_empty_count} string(s) — somente as completamente vazias\n\n"
            "Nada será salvo até você clicar em 💾 Salvar."
        )
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        btn_all   = msg_box.addButton("Traduzir tudo",   QMessageBox.ButtonRole.AcceptRole)
        btn_empty = msg_box.addButton("Apenas vazios",   QMessageBox.ButtonRole.ActionRole)
        msg_box.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()

        clicked = msg_box.clickedButton()
        if clicked is btn_all:
            only_empty = False
        elif clicked is btn_empty:
            only_empty = True
            if only_empty_count == 0:
                self._set_status("Nenhuma string completamente vazia para traduzir.")
                return
        else:
            return

        eligible = [
            entry
            for entry in self._tsfile.entries
            if entry.status == "empty"
            or (not only_empty and entry.status == "unfinished")
        ]
        self._auto_translate_target = (
            self._current["code"],
            self._tsfile.path,
            id(self._tsfile),
        )
        self._auto_translate_entry_ids = {entry.entry_id for entry in eligible}
        self._set_busy(True, "auto_tr")
        w = _AutoTranslateWorker(
            eligible,
            self._current["name"],
            self._current["code"],
            api_key,
            only_empty,
        )
        w.signals.done.connect(self._auto_translate_done)
        self._pool.start(w)

    @Slot(bool, str, str)
    def _auto_translate_done(self, ok: bool, message: str, json_results: str):
        self._set_busy(False, "auto_tr")
        target = self._auto_translate_target
        eligible_entry_ids = self._auto_translate_entry_ids
        self._auto_translate_target = None
        self._auto_translate_entry_ids = set()
        current_target = (
            self._current["code"],
            self._tsfile.path,
            id(self._tsfile),
        ) if self._current and self._tsfile else None
        if target is None or current_target != target:
            self._set_status(
                "Resultado descartado: o idioma de destino foi alterado.",
                error=True,
            )
            return
        if not ok:
            self._set_status(f"Erro no preenchimento: {message}", error=True)
            QMessageBox.warning(self, "Erro no preenchimento", message)
            return

        try:
            results: dict = json.loads(json_results)
        except json.JSONDecodeError:
            self._set_status("Erro: não foi possível processar o resultado.", error=True)
            return

        if not results:
            self._set_status("Nenhuma tradução retornada.")
            return

        applied = 0
        rejected = 0
        for idx, entry in enumerate(self._tsfile.entries):
            if (
                entry.entry_id not in eligible_entry_ids
                or entry.entry_id not in results
            ):
                continue
            translated = _validated_translation(entry, results[entry.entry_id])
            if translated is None:
                rejected += 1
                continue
            self._model.update_entry(idx, translated)
            applied += 1

        self._proxy.invalidateFilter()
        self._table.viewport().update()
        self._update_stats()

        has_empty = any(e.status in ("empty", "unfinished") for e in self._tsfile.entries)
        self._btn_auto_tr.setEnabled(has_empty)

        self._set_status(
            f"✦  {applied} string(s) preenchida(s) em {self._current['name']} — "
            f"{rejected} resultado(s) rejeitado(s) por formato ou placeholders. "
            "Revise e clique em 💾 Salvar quando terminar."
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool, which: str):
        self._busy_operation = which if busy else None
        self._combo.setEnabled(not busy)
        self._btn_new.setEnabled(not busy)
        self._table.setEnabled(not busy)
        self._detail.setEnabled(not busy)
        self._btn_lu.setEnabled(not busy and self._current is not None)
        self._btn_lr.setEnabled(
            not busy and self._current is not None and bool(self._current.get("ts"))
        )
        self._btn_sv.setEnabled(not busy and self._model.dirty)
        has_pending = bool(
            self._tsfile
            and any(
                entry.status in ("empty", "unfinished")
                for entry in self._tsfile.entries
            )
        )
        self._btn_auto_tr.setEnabled(
            not busy
            and has_pending
            and self._current is not None
            and not self._current["is_source"]
        )
        if which == "lu":
            self._btn_lu.setText("⏳  Rodando lupdate…" if busy
                                  else "↻  Extrair strings  (lupdate)")
        elif which == "lr":
            self._btn_lr.setText("⏳  Compilando…" if busy
                                  else "▶  Compilar .qm  (lrelease)")
        elif which == "auto_tr":
            self._btn_auto_tr.setText("⏳  Traduzindo…" if busy
                                      else "✦  Preencher vazios")

    def _update_stats(self):
        s = self._model.stats()
        warn_part = (f"  ·  <span style='color:{C['orange']}'>"
                     f"{s['warnings']} ⚠</span>" if s["warnings"] else "")
        self._stats_lbl.setText(
            f"<span style='color:{C['green']}'>{s['ok']} ok</span>  ·  "
            f"<span style='color:{C['red']}'>{s['pending']} pendentes</span>  ·  "
            f"<span style='color:{C['yellow']}'>{s['same']} iguais</span>"
            f"{warn_part}")
        self._progress.setText(f"{s['ok']} / {s['total']}")

    def _set_status(self, msg: str, error: bool = False, info: bool = False):
        color = C["red"] if error else (C["accent"] if info else C["text_sec"])
        self._status.setStyleSheet(f"color:{color};padding-left:8px;")
        self._status.setText(msg)
        if not error:
            QTimer.singleShot(10000, lambda: self._status.setText("Pronto"))

    def closeEvent(self, ev):
        if self._model.dirty:
            r = QMessageBox.question(
                self, "Salvar?",
                "Há traduções não salvas. Salvar antes de sair?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Save:
                if self._do_save():
                    ev.accept()
                else:
                    ev.ignore()
            elif r == QMessageBox.StandardButton.Discard:
                ev.accept()
            else:
                ev.ignore()
        else:
            ev.accept()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Solin Translation Editor")
    app.setStyleSheet(SS)
    os.makedirs(_TRANS_DIR, exist_ok=True)
    win = TranslationEditor()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
