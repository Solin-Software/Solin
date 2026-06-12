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
    Ele não precisa de .ts nem de .qm — é o fallback nativo do Qt.
  • Somente os idiomas de destino (pt_BR, es, ja, …) precisam de .ts/.qm.

Quirks conhecidos do toolchain (tratados automaticamente):
  • pyside6-lupdate <= 6.7.x emite <n>ContextName</n> dentro de <context>
    em vez do tag canônico <name>ContextName</name> exigido pelo lrelease.
    A função _sanitize_ts() é chamada automaticamente após todo lupdate e
    antes de todo save(), tornando o pipeline robusto a qualquer versão.
    Ref: https://bugreports.qt.io/browse/PYSIDE-2418

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
from typing import Optional

from dotenv import load_dotenv
load_dotenv(override=True)

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
_TRANS_DIR    = os.path.join(_PROJECT_ROOT, "translations")
_LANG_DIR     = os.path.join(_TRANS_DIR, "locales")

# ── Source language — never needs .ts/.qm ─────────────────────────────────────
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

_JW_TERMINOLOGY: dict[str, dict[str, str]] = {
"S": {
        "Original_Songs": "Canciones Originales",
        "Songs": "Canciones",
        "Song": "Canción",
        "public_talk": "Discurso Público",
        "Treasures Talk": "Discurso de Tesoros",
        "Watchtower Study": "Estudio de La Atalaya",
        "WATCHTOWER STUDY": "ESTUDIO DE LA ATALAYA",
        "Memorial": "Conmemoración",
        "Memorial of Jesus’ Death": "Conmemoración de la muerte de Jesús",
        "TREASURES FROM GOD'S WORD": "TESOROS DE LA BIBLIA",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "SEAMOS MEJORES MAESTROS",
        "LIVING AS CHRISTIANS": "NUESTRA VIDA CRISTIANA",
        "Life & Ministry": "Vida y Ministerio",
        "Opening Comments": "Palabras de introducción",
        "Spiritual Gems": "Busquemos perlas escondidas",
        "Bible Reading": "Lectura de la Biblia",
        "Congregation Bible Study": "Estudio bíblico de la congregación",
        "Concluding Comments": "Palabras de conclusión"
    },
    "F": {
        "Original_Songs": "Chansons",
        "Songs": "Cantiques",
        "Song": "Cantique",
        "public_talk": "Discours Public",
        "Treasures Talk": "Discours des joyaux",
        "Watchtower Study": "Étude de La Tour",
        "WATCHTOWER STUDY": "ÉTUDE DE LA TOUR DE GARDE",
        "Memorial": "Commémoration",
        "Memorial of Jesus’ Death": "Commémoration de la mort de Jésus",
        "TREASURES FROM GOD'S WORD": "JOYAUX DE LA PAROLE DE DIEU",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "APPLIQUE-TOI AU MINISTÈRE",
        "LIVING AS CHRISTIANS": "VIE CHRÉTIENNE",
        "Life & Ministry": "Vie et ministère",
        "Opening Comments": "Paroles d’introduction",
        "Spiritual Gems": "Perles spirituelles",
        "Bible Reading": "Lecture de la Bible",
        "Congregation Bible Study": "Étude biblique de l’assemblée",
        "Concluding Comments": "Paroles de conclusion"
    },
    "I": {
        "Original_Songs": "Canzoni",
        "Songs": "Cantici",
        "Song": "Cantico",
        "public_talk": "Discorso Pubblico",
        "Treasures Talk": "Discorso dei tesori",
        "Watchtower Study": "Studio della Torre di Guardia",
        "WATCHTOWER STUDY": "STUDIO DELLA TORRE DI GUARDIA",
        "Memorial": "Commemorazione",
        "Memorial of Jesus’ Death": "Commemorazione della morte di Gesù",
        "TREASURES FROM GOD'S WORD": "TESORI DELLA PAROLA DI DIO",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "EFFICACI NEL MINISTERO",
        "LIVING AS CHRISTIANS": "VITA CRISTIANA",
        "Life & Ministry": "Vita e ministero",
        "Opening Comments": "Commenti introduttivi",
        "Spiritual Gems": "Gemme spirituali",
        "Bible Reading": "Lettura biblica",
        "Congregation Bible Study": "Studio biblico di congregazione",
        "Concluding Comments": "Commenti conclusivi"
    },
    "J": {
        "Original_Songs": "オリジナルソング",
        "Songs": "歌",
        "Song": "歌",
        "public_talk": "公開講演",
        "Treasures Talk": "宝の講演",
        "Watchtower Study": "「ものみの塔」研究",
        "WATCHTOWER STUDY": "「ものみの塔」研究",
        "Memorial": "記念式",
        "Memorial of Jesus’ Death": "イエスの死の記念式",
        "TREASURES FROM GOD'S WORD": "神の言葉の宝",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "野外奉仕に励む",
        "LIVING AS CHRISTIANS": "クリスチャンとして生活する",
        "Life & Ministry": "生活と奉仕",
        "Opening Comments": "開会の言葉",
        "Spiritual Gems": "宝石を探し出す",
        "Bible Reading": "聖書朗読",
        "Congregation Bible Study": "会衆の聖書研究",
        "Concluding Comments": "閉会の言葉"
    },
    "T": {
        "Original_Songs": "Clipes Musicais",
        "Songs": "Cânticos",
        "Song": "Cântico",
        "public_talk": "Discurso Público",
        "Treasures Talk": "Discurso de Tesouros",
        "Watchtower Study": "Estudo de A Sentinela",
        "WATCHTOWER STUDY": "ESTUDO DE A SENTINELA",
        "Memorial": "Celebração",
        "Memorial of Jesus’ Death": "Celebração da morte de Jesus",
        "TREASURES FROM GOD'S WORD": "TESOUROS DA PALAVRA DE DEUS",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "FAÇA SEU MELHOR NO MINISTÉRIO",
        "LIVING AS CHRISTIANS": "NOSSA VIDA CRISTÃ",
        "Life & Ministry": "Vida e Ministério",
        "Opening Comments": "Comentários iniciais",
        "Spiritual Gems": "Joias espirituais",
        "Bible Reading": "Leitura da Bíblia",
        "Congregation Bible Study": "Estudo bíblico de congregação",
        "Concluding Comments": "Comentários finais"
    },
    "CHS": {
        "Original_Songs": "原创歌曲",
        "Songs": "诗歌",
        "Song": "诗歌",
        "public_talk": "公众演讲",
        "Treasures Talk": "宝藏演讲",
        "Watchtower Study": "守望台研读",
        "WATCHTOWER STUDY": "守望台研读",
        "Memorial": "纪念",
        "Memorial of Jesus’ Death": "耶稣死亡的纪念",
        "TREASURES FROM GOD'S WORD": "上帝话语的宝藏",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "用心准备传道工作",
        "LIVING AS CHRISTIANS": "基督徒的生活",
        "Life & Ministry": "传道与生活聚会",
        "Opening Comments": "开场白",
        "Spiritual Gems": "经文宝石",
        "Bible Reading": "经文朗读",
        "Congregation Bible Study": "会众研经班",
        "Concluding Comments": "结语"
    },
    "U": {
        "Original_Songs": "Оригинальные песни",
        "Songs": "Песни",
        "Song": "Песня",
        "public_talk": "Публичная речь",
        "Treasures Talk": "Речь о сокровищах",
        "Watchtower Study": "Сторожевая башня",
        "WATCHTOWER STUDY": "СТОРОЖЕВАЯ БАШНЯ",
        "Memorial": "Вечеря воспоминания",
        "Memorial of Jesus’ Death": "Вечеря воспоминания смерти Иисуса Христа",
        "TREASURES FROM GOD'S WORD": "СОКРОВИЩА ИЗ СЛОВА БОГА",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "ОТТАЧИВАЕМ НАВЫКИ СЛУЖЕНИЯ",
        "LIVING AS CHRISTIANS": "ХРИСТИАНСКАЯ ЖИЗНЬ",
        "Life & Ministry": "Жизнь и служение",
        "Opening Comments": "Вступительные слова",
        "Spiritual Gems": "Духовные жемчужины",
        "Bible Reading": "Чтение Библии",
        "Congregation Bible Study": "Изучение Библии в собрании",
        "Concluding Comments": "Заключительные слова"
    },
    "KO": {
        "Original_Songs": "오리지널 노래",
        "Songs": "노래",
        "Song": "노래",
        "public_talk": "공개 강연",
        "Treasures Talk": "보물 강연",
        "Watchtower Study": "파수대 연구",
        "WATCHTOWER STUDY": "파수대 연구",
        "Memorial": "기념식",
        "Memorial of Jesus’ Death": "예수의 죽음의 기념식",
        "TREASURES FROM GOD'S WORD": "성경에 담긴 보물",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "야외 봉사에 힘쓰십시오",
        "LIVING AS CHRISTIANS": "그리스도인 생활",
        "Life & Ministry": "생활과 봉사",
        "Opening Comments": "소개말",
        "Spiritual Gems": "영적 보물 찾기",
        "Bible Reading": "성경 낭독",
        "Congregation Bible Study": "회중 성서 연구",
        "Concluding Comments": "맺음말"
    },
    "Z": {
        "Original_Songs": "Originalsånger",
        "Songs": "Sånger",
        "Song": "Sång",
        "public_talk": "Offentligt tal",
        "Treasures Talk": "Höjdpunkter-tal",
        "Watchtower Study": "Vakttornsstudiet",
        "WATCHTOWER STUDY": "VAKTTORNSSTUDIET",
        "Memorial": "Minnesmåltiden",
        "Memorial of Jesus’ Death": "Minnesmåltid till minne av Jesu död",
        "TREASURES FROM GOD'S WORD": "HÖJDPUNKTER FRÅN BIBELN",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "ÖVNING FÖR TJÄNSTEN",
        "LIVING AS CHRISTIANS": "LIVET SOM KRISTEN",
        "Life & Ministry": "Livet och tjänsten som kristen",
        "Opening Comments": "Inledande ord",
        "Spiritual Gems": "Andliga guldkorn",
        "Bible Reading": "Bibelläsning",
        "Congregation Bible Study": "Församlingens bibelstudium",
        "Concluding Comments": "Avslutande ord"
    },
    "X": {
        "Original_Songs": "Besondere Lieder",
        "Songs": "Lieder",
        "Song": "Lied",
        "public_talk": "Öffentlicher Vortrag",
        "Treasures Talk": "Schätze-Vortrag",
        "Watchtower Study": "Wachtturm-Studium",
        "WATCHTOWER STUDY": "WACHTTURM-STUDIUM",
        "Memorial": "Gedächtnismahl",
        "Memorial of Jesus’ Death": "Gedächtnismahl des Todes Jesu",
        "TREASURES FROM GOD'S WORD": "SCHÄTZE AUS GOTTES WORT",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "UNS IM DIENST VERBESSERN",
        "LIVING AS CHRISTIANS": "UNSER LEBEN ALS CHRIST",
        "Life & Ministry": "Leben und Dienst",
        "Opening Comments": "Einleitende Worte",
        "Spiritual Gems": "Nach geistigen Schätzen graben",
        "Bible Reading": "Bibellesung",
        "Congregation Bible Study": "Versammlungs­bibelstudium",
        "Concluding Comments": "Schlussworte"
    },
    "P": {
        "Original_Songs": "Piosenki",
        "Songs": "Pieśni",
        "Song": "Pieśń",
        "public_talk": "Wykład publiczny",
        "Treasures Talk": "Wykład ze skarbów",
        "Watchtower Study": "Studium Strażnicy",
        "WATCHTOWER STUDY": "STUDIUM STRAŻNICY",
        "Memorial": "Pamiątka",
        "Memorial of Jesus’ Death": "Pamiątka śmierci Jezusa",
        "TREASURES FROM GOD'S WORD": "SKARBY ZE SŁOWA BOŻEGO",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "ULEPSZAJMY SWOJĄ SŁUŻBĘ",
        "LIVING AS CHRISTIANS": "CHRZEŚCIJAŃSKI TRYB ŻYCIA",
        "Life & Ministry": "Życie i służba",
        "Opening Comments": "Uwagi wstępne",
        "Spiritual Gems": "Wyszukujemy duchowe skarby",
        "Bible Reading": "Czytanie Biblii",
        "Congregation Bible Study": "Zborowe studium Biblii",
        "Concluding Comments": "Uwagi końcowe"
    },
    "FI": {
        "Original_Songs": "ALKUPERÄISMUSIIKKI",
        "Songs": "Laulut",
        "Song": "Laulu",
        "public_talk": "julkinen puhe",
        "Treasures Talk": "Aarteita-puhe",
        "Watchtower Study": "Vartiotornin tutkistelu",
        "WATCHTOWER STUDY": "VARTIOTORNIN TUTKISTELU",
        "Memorial": "Muistoateria",
        "Memorial of Jesus’ Death": "Muistoateria Jeesuksen kuolemasta",
        "TREASURES FROM GOD'S WORD": "JUMALAN SANAN AARTEITA",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "VALMENNUSTA KENTTÄTYÖHÖN",
        "LIVING AS CHRISTIANS": "ELÄMÄ KRISTITTYNÄ",
        "Life & Ministry": "Elämä ja palvelus",
        "Opening Comments": "Alkusanat",
        "Spiritual Gems": "Hengellisiä helmiä",
        "Bible Reading": "Raamatun lukeminen",
        "Congregation Bible Study": "Seurakunnan raamatuntutkistelu",
        "Concluding Comments": "Loppusanat"
    },
    "TG": {
        "Original_Songs": "Mga Original Song",
        "Songs": "Mga Awit",
        "Song": "Awit",
        "public_talk": "Pahayag Pangmadla",
        "Treasures Talk": "Pahayag sa Kayamanan",
        "Watchtower Study": "Pag-aaral sa Bantayan",
        "WATCHTOWER STUDY": "PAG-AARAL SA BANTAYAN",
        "Memorial": "Paggunita",
        "Memorial of Jesus’ Death": "Paggunita sa Kamatayan ni Jesus",
        "TREASURES FROM GOD'S WORD": "KAYAMANAN MULA SA SALITA NG DIYOS",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "MAGING MAHUSAY SA MINISTERYO",
        "LIVING AS CHRISTIANS": "PAMUMUHAY BILANG KRISTIYANO",
        "Life & Ministry": "Buhay at Ministeryo",
        "Opening Comments": "Pambungad na Komento",
        "Spiritual Gems": "Espirituwal na Hiyas",
        "Bible Reading": "Pagbabasa ng Bibliya",
        "Congregation Bible Study": "Pag-aaral ng Kongregasyon sa Bibliya",
        "Concluding Comments": "Pangwakas na Komento"
    },
    "O": {
        "Original Songs": "Original songs",
        "Songs": "Liederen",
        "Song": "Lied",
        "Public talk": "Openbare lezing",
        "Treasures Talk": "Schatten-lezing",
        "Watchtower Study": "Wachttoren-studie",
        "WATCHTOWER STUDY": "WACHTTOREN-STUDIE",
        "Memorial": "Herdenking",
        "Memorial of Jesus’ Death": "Herdenking van Jezus' dood",
        "TREASURES FROM GOD'S WORD": "SCHATTEN UIT GODS WOORD",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "LEG JE TOE OP DE VELDDIENST",
        "LIVING AS CHRISTIANS": "LEVEN ALS CHRISTENEN",
        "Life & Ministry": "Leven en dienen",
        "Opening Comments": "Inleiding",
        "Spiritual Gems": "Geestelijke juweeltjes",
        "Bible Reading": "Bijbellezen",
        "Congregation Bible Study": "Gemeentebijbelstudie",
        "Concluding Comments": "Slotopmerkingen"
    },
    "IN": {
        "Original Songs": "Lagu Baru",
        "Songs": "Lagu-Lagu",
        "Song": "Lagu",
        "Public talk": "Khotbah Umum",
        "Treasures Talk": "Khotbah Harta",
        "Watchtower Study": "Pelajaran Menara Pengawal",
        "WATCHTOWER STUDY": "PELAJARAN MENARA PENGAWAL",
        "Memorial": "Peringatan",
        "Memorial of Jesus’ Death": "Peringatan Kematian Yesus",
        "TREASURES FROM GOD'S WORD": "HARTA DALAM FIRMAN ALLAH",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "BERSEMANGATLAH DALAM PELAYANAN",
        "LIVING AS CHRISTIANS": "KEHIDUPAN KRISTEN",
        "Life & Ministry": "Pelayanan dan Kehidupan",
        "Opening Comments": "Bagian Pembuka",
        "Spiritual Gems": "Permata Rohani",
        "Bible Reading": "Pembacaan Alkitab",
        "Congregation Bible Study": "Pelajaran Alkitab Sidang",
        "Concluding Comments": "Bagian Penutup"
    },
    "K": {
        "Original Songs": "РІЗНІ ПІСНІ",
        "Songs": "Пісні",
        "Song": "Пісня",
        "Public talk": "Публічна промова",
        "Treasures Talk": "Промова зі скарбів",
        "Watchtower Study": "Вивчення «Вартової башти»",
        "WATCHTOWER STUDY": "ВИВЧЕННЯ «ВАРТОВОЇ БАШТИ»",
        "Memorial": "Спомин",
        "Memorial of Jesus’ Death": "Спомин Ісусової смерті",
        "TREASURES FROM GOD'S WORD": "СКАРБИ З БОЖОГО СЛОВА",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "ВДОСКОНАЛЮЙМО СВОЄ СЛУЖІННЯ",
        "LIVING AS CHRISTIANS": "ХРИСТИЯНСЬКЕ ЖИТТЯ",
        "Life & Ministry": "Життя і служіння",
        "Opening Comments": "Вступні слова",
        "Spiritual Gems": "Духовні перлини",
        "Bible Reading": "Читання Біблії",
        "Congregation Bible Study": "Вивчення Біблії у зборі",
        "Concluding Comments": "Кінцеві слова"
    },
    "SW": {
        "Original Songs": "Nyimbo Zilizotungwa",
        "Songs": "Nyimbo",
        "Song": "Wimbo",
        "Public talk": "Hotuba ya Watu Wote",
        "Treasures Talk": "Hotuba ya Hazina",
        "Watchtower Study": "Funzo la Mnara wa Mlinzi",
        "WATCHTOWER STUDY": "FUNZO LA MNARA WA MLINZI",
        "Memorial": "Ukumbusho",
        "Memorial of Jesus’ Death": "Ukumbusho wa Kifo cha Yesu",
        "TREASURES FROM GOD'S WORD": "HAZINA ZA NENO LA MUNGU",
        "APPLY YOURSELF TO THE FIELD MINISTRY": "BORESHA HUDUMA YAKO",
        "LIVING AS CHRISTIANS": "MAISHA YA MKRISTO",
        "Life & Ministry": "Huduma na Maisha",
        "Opening Comments": "Utangulizi",
        "Spiritual Gems": "Hazina za Kiroho",
        "Bible Reading": "Usomaji wa Biblia",
        "Congregation Bible Study": "Funzo la Biblia la Kutaniko",
        "Concluding Comments": "Umalizio"
    }
}


def _build_ts_system_prompt(lang_name: str, lang_code: str, api_code: str) -> str:
    glossary_section = ""
    terms = _JW_TERMINOLOGY.get(api_code.upper())
    if terms:
        term_lines = "\n".join(
            '  "{}" -> "{}"'.format(src, tgt) for src, tgt in terms.items()
        )
        glossary_section = (
            "\n\nOFFICIAL JW GLOSSARY — always use these exact terms for "
            f"{lang_name} (api_code: {api_code}):\n{term_lines}"
        )

    return (
        "You are a professional translator specialised in institutional texts "
        "for Jehovah's Witnesses.\n\n"
        "STYLE RULES:\n"
        "- Use neutral, impersonal, formal language that still reads naturally.\n"
        "- Perhaps some words can be kept in the translation, such as “Changelog” in Brazilian Portuguese.\n"
        "- Avoid first- and second-person pronouns; prefer impersonal constructions.\n"
        "- Treat personal names used purely as illustrative placeholders (e.g., “John Doe”) as non-specific and adapt or replace them with natural equivalents in the target language when appropriate; however, preserve real person names, brand names, and product names unchanged, unless a widely accepted localized form exists. \n"
        "- The translation must sound native and idiomatic, never word-for-word literal.\n"
        "- Preserve the urgency and tone of the source.\n"
        "- Keep any {variable} or %n placeholders exactly as-is.\n"
        "- For entries marked with ' | (plural)': return a JSON array of strings for the plural forms of that language.\n"
        "- For plain entries return a plain string.\n"
        + glossary_section
        + f"\n\nTARGET LANGUAGE: {lang_name} ({lang_code})\n\n"
        "RESPONSE FORMAT:\n"
        "Return ONLY a valid JSON object mapping each original English source key "
        "to its translation (string or array). No markdown, no code fences, no explanation."
    )


def _run_ts_auto_translate(
    entries: list["Entry"],
    lang_name: str,
    lang_code: str,
    api_code: str,
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

    client = genai.Client(api_key=api_key)
    system_prompt = _build_ts_system_prompt(lang_name, lang_code, api_code)

    BATCH_SIZE = 60
    all_results = {}
    total_batches = (len(empty) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(empty), BATCH_SIZE):
        chunk = empty[i : i + BATCH_SIZE]
        batch: dict[str, str] = {}
        
        for e in chunk:
            key = e.source
            if e.is_plural:
                batch[key] = f"{e.source} | (plural)"
            else:
                batch[key] = e.source

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
        dirs[:] = [
            d for d in dirs 
            if d not in skip and not d.startswith("venv") and not d.startswith("env")
        ]
        
        # 2. Segurança extra: ignora se por acaso entrar em um site-packages
        if "site-packages" in dirpath.lower():
            continue
            
        for f in files:
            if f.startswith("test_"):
                continue
            if f.endswith((".py", ".qml")):
                result.append(os.path.join(dirpath, f))
                
    return result


def _sanitize_ts(ts_path: str) -> None:
    """
    Corrige bugs conhecidos do pyside6-lupdate no arquivo .ts gerado.

    Bug #1 — <n> em vez de <name> (PySide6 <= 6.7.x):
        O lupdate emite <n>ContextName</n> dentro de <context>, mas o
        lrelease (e o padrão Qt TS) exige <name>ContextName</name>.
        Referência: https://bugreports.qt.io/browse/PYSIDE-2418

    A correção é feita via substituição de texto puro (não via ET.parse)
    para preservar exatamente o restante do conteúdo sem re-serialização.
    O arquivo só é reescrito se realmente houver algo a corrigir.
    """
    with open(ts_path, "r", encoding="utf-8") as fh:
        original = fh.read()

    # Substitui <n> e </n> apenas quando forem a tag inteira
    # (não dentro de outras palavras como <name>, <next>, etc.)
    fixed = re.sub(r'<n>(?=[^/])', '<name>', original)
    fixed = re.sub(r'</n>',        '</name>', fixed)

    if fixed != original:
        with open(ts_path, "w", encoding="utf-8") as fh:
            fh.write(fixed)


def run_lupdate(project_root: str, ts_path: str) -> tuple[bool, str]:
    import shutil
    import importlib.util

    # 1. Encontra o executável garantindo a extensão .exe no Windows
    exe_name = "pyside6-lupdate.exe" if os.name == 'nt' else "pyside6-lupdate"
    cmd_exe = shutil.which(exe_name)
    
    if not cmd_exe:
        spec = importlib.util.find_spec("PySide6")
        if spec and spec.submodule_search_locations:
            pdir = list(spec.submodule_search_locations)[0]
            int_exe = os.path.join(pdir, "lupdate.exe" if os.name == 'nt' else "lupdate")
            if os.path.isfile(int_exe):
                cmd_exe = int_exe

    if not cmd_exe:
        return False, "Executável do lupdate (.exe) não encontrado no ambiente Anaconda/Python."

    source_files = _collect_translation_sources(project_root)
    if not source_files:
        return False, f"Nenhum arquivo .py/.qml encontrado em: {project_root}"

    os.makedirs(os.path.dirname(ts_path), exist_ok=True)

    # 2. Cria um arquivo de texto comum (.lst) em vez de .pro
    # O lupdate lê a lista diretamente sem tentar acionar o qmake
    lst_name = "_temp_files.lst"
    lst_path = os.path.join(project_root, lst_name)

    try:
        with open(lst_path, "w", encoding="utf-8") as f:
            for source in source_files:
                # Pode mandar o caminho absoluto direto, não tem problema!
                rel_path = os.path.relpath(source, project_root).replace('\\', '/')
                f.write(f"{rel_path}\n")

        # 3. O "pulo do gato": usamos o "@" antes do nome do arquivo
        cmd = [cmd_exe, f"@{lst_path}", "-ts", ts_path]
        
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=60, cwd=project_root)
        
        out = (r.stdout + "\n" + r.stderr).strip()
        
        if r.returncode != 0 or not os.path.isfile(ts_path):
            return False, f"Falha no lupdate (código {r.returncode}):\n{out}\nExecutável: {cmd_exe}"
            
        _sanitize_ts(ts_path)
        return True, out

    except subprocess.TimeoutExpired:
        return False, "Timeout (>60s). Tente rodar manualmente no terminal."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Erro executando o comando: {exc}"
    finally:
        # 4. Exclui o arquivo de lista temporário
        if os.path.exists(lst_path):
            try: os.remove(lst_path)
            except OSError: pass


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
    def __init__(self, entries, lang_name, lang_code, api_code, api_key,
                 only_empty: bool = False):
        super().__init__()
        self.signals    = _SigAT()
        self._entries   = entries
        self._lang_name = lang_name
        self._lang_code = lang_code
        self._api_code  = api_code
        self._api_key   = api_key
        self._only_empty = only_empty

    @Slot()
    def run(self):
        ok, msg, results = _run_ts_auto_translate(
            self._entries, self._lang_name, self._lang_code,
            self._api_code, self._api_key, self._only_empty,
        )
        self.signals.done.emit(ok, msg, json.dumps(results))


# ══════════════════════════════════════════════════════════════════════════════
# .ts I/O
# ══════════════════════════════════════════════════════════════════════════════

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
    def __init__(self, elem, context, source, translation, finished,
                 is_plural: bool = False, forms: list[str] | None = None):
        self._elem        = elem
        self.context      = context
        self.source       = source
        self.translation  = translation
        self.finished     = finished
        self.modified     = False
        self.is_plural    = is_plural
        self.forms: list[str] = forms or []

    @property
    def status(self):
        if self.is_plural:
            if not self.forms or all(f == "" for f in self.forms):
                return "empty"
            if any(f == "" for f in self.forms):
                return "unfinished"
            if not self.finished:
                return "unfinished"
            return "ok"
        if not self.translation:            return "empty"
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
    def vars_missing(self) -> list[str]:
        """Variáveis %n/%1/%2 ou {x} presentes no source mas ausentes na tradução."""
        src_vars = set(re.findall(r"\{(\w+)\}", self.source))
        if self.is_plural:
            all_tr = " ".join(self.forms)
            tr_vars = set(re.findall(r"\{(\w+)\}", all_tr))
        else:
            tr_vars = set(re.findall(r"\{(\w+)\}", self.translation))
        return sorted(src_vars - tr_vars)


class TsFileSaveError(RuntimeError):
    """The translation file could not be saved atomically."""


class TsFile:
    def __init__(self, path: str):
        self.path  = path
        # Sanitiza antes de parsear para garantir XML válido independente
        # da versão do lupdate que gerou o arquivo.
        _sanitize_ts(path)
        self._tree = ET.parse(path)
        self._root = self._tree.getroot()
        self.entries: list[Entry] = []
        self._parse()

    def _parse(self) -> None:
        self.entries.clear()
        for ctx in self._root.iter("context"):
            name_el  = ctx.find("name")
            ctx_name = (name_el.text or "").strip() if name_el is not None else ""

            for msg in ctx.findall("message"):
                src_el = msg.find("source")
                tr_el  = msg.find("translation")
                if src_el is None or tr_el is None:
                    continue
                source = (src_el.text or "").strip()

                # Se for obsoleta, pula (mas preserva no XML)
                if tr_el.get("type") == "obsolete":
                    continue

                finished = tr_el.get("type") != "unfinished"

                # ── Detecta entrada plural ────────────────────────────────────
                is_plural = msg.get("numerus") == "yes"
                forms: list[str] = []
                if is_plural:
                    for nf in tr_el.findall("numerusform"):
                        forms.append((nf.text or "").strip())
                    translation = ""   # não usado para plurais
                else:
                    translation = (tr_el.text or "").strip()

                # Prioridade de contexto: comment > <n> > location
                cmt = msg.find("comment")
                if cmt is not None and cmt.text:
                    ctx_str = cmt.text.strip()
                elif ctx_name:
                    ctx_str = ctx_name
                else:
                    loc = msg.find("location")
                    ctx_str = (
                        f"{os.path.basename(loc.get('filename', ''))}"
                        f":{loc.get('line', '')}"
                        if loc is not None else ""
                    )

                self.entries.append(Entry(
                    msg, ctx_str, source, translation, finished,
                    is_plural=is_plural, forms=forms,
                ))
    def save(self) -> None:
        """
        Persiste o .ts de forma segura:
          1. Serializa para arquivo temporário na mesma pasta (evita
             corrupção se o processo for interrompido).
          2. Aplica _sanitize_ts() no temporário (garante que qualquer
             re-serialização pelo ET não reintroduza tags inválidas).
          3. Faz rename atômico sobre o arquivo original.
        """
        tmp_path = self.path + ".tmp"
        try:
            ET.indent(self._tree, space="  ")
            self._tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
            _sanitize_ts(tmp_path)
            os.replace(tmp_path, self.path)   # atômico em todos os SO modernos
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
            if e.vars_missing:      return QColor(C["orange"])

        if role == Qt.ItemDataRole.BackgroundRole:
            if e.modified:     return QColor(C["accent_dim"])

        if role == Qt.ItemDataRole.ToolTipRole:
            if e.vars_missing:
                return f"⚠ Variáveis ausentes na tradução: {', '.join(e.vars_missing)}"

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
            if new_forms == e.forms:
                return
            e.forms    = new_forms
            e.finished = any(f.strip() for f in new_forms)
            e.modified = True
            tr_el = e._elem.find("translation")
            if tr_el is not None:
                # Atualiza cada <numerusform> individualmente
                existing = tr_el.findall("numerusform")
                for i, form_text in enumerate(new_forms):
                    if i < len(existing):
                        existing[i].text = form_text or None
                    else:
                        nf = ET.SubElement(tr_el, "numerusform")
                        nf.text = form_text or None
                # Remove formas extras se o novo valor tiver menos
                for nf in existing[len(new_forms):]:
                    tr_el.remove(nf)
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
        warn = sum(1 for e in self._rows if e.vars_missing)
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
            return self._txt in f"{e.context} {e.source} {e.translation}".lower()
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

        self._ctx.setText(entry.context or "—")
        self._src.setPlainText(entry.source)

        if entry.is_plural:
            # Garante que sempre há pelo menos 2 formas no UI
            forms = entry.forms if entry.forms else ["", ""]
            self._show_plural_mode(forms)
            self._copy_btn.hide()
        else:
            self._show_simple_mode()
            self._tr.setPlainText(entry.translation)

        self._save_btn.setEnabled(False)
        self._update_warn(entry)
        self._loading = False

    def clear(self):
        self._row, self._loading = None, True
        self._ctx.setText("—")
        self._src.clear()
        self._show_simple_mode()
        self._tr.clear()
        self._save_btn.setEnabled(False)
        self._warn.setText("")
        self._loading = False

    # ── internos ──────────────────────────────────────────────────────────

    def _update_warn(self, entry: "Entry"):
        mv = entry.vars_missing
        self._warn.setText(
            f"⚠ Variáveis ausentes: {', '.join(mv)}" if mv else "")

    def _do_copy_source(self):
        """Copia o source para o campo de tradução simples."""
        self._tr.setPlainText(self._src.toPlainText())

    def _changed(self):
        if not self._loading:
            self._save_btn.setEnabled(True)

    def _commit(self):
        if self._row is None:
            return
        if self._plural_edits:
            # Coleta todas as formas
            value: list[str] = [ed.toPlainText().strip() for ed in self._plural_edits]
        else:
            value: str = self._tr.toPlainText().strip()
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

        self._btn_lu.setEnabled(True)
        self._btn_lr.setEnabled(True)
        self._btn_auto_tr.setEnabled(False)

        ts = lang.get("ts")
        if not ts or not os.path.isfile(ts):
            self._tsfile = None
            self._model.load([])
            self._detail.clear()
            self._update_stats()
            self._set_status(
                f"solin_{lang['code']}.ts não existe — "
                f"clique em '↻ Extrair strings' para criá-lo.")
            return

        try:
            self._tsfile = TsFile(ts)
        except (OSError, ET.ParseError, ValueError) as exc:
            self._set_status(f"Erro ao abrir {ts}: {exc}", error=True)
            return

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
                self._do_save()
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
        if not self._current:
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
            if r == QMessageBox.StandardButton.Save:   self._do_save()

        self._set_busy(True, "lu")
        w = _Worker(run_lupdate, _PROJECT_ROOT, ts_path)
        self._pending_ts_path = ts_path
        w.signals.done.connect(self._lu_done)
        self._pool.start(w)

    @Slot(bool, str)
    def _lu_done(self, ok: bool, output: str):
        ts_path = getattr(self, "_pending_ts_path", "")
        self._set_busy(False, "lu")

        # Informa se a sanitização corrigiu o arquivo
        note = ""
        if ok and os.path.isfile(ts_path):
            note = (
                "\n\n─────────────────────────────\n"
                "\u2139\ufe0f  Sanitização automática aplicada: quaisquer tags\n"
                "   inválidas geradas pelo lupdate foram corrigidas\n"
                "   (ex: <n> \u2192 <name>).  O arquivo está pronto para uso."
            )

        OutputDialog("lupdate — " + ("OK" if ok else "Falhou"),
                     output + note, ok, self).exec()

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

    def _do_save(self):
        if not self._tsfile:
            return
        try:
            self._tsfile.save()
            self._model.mark_clean()
            self._set_status(f"✓  Salvo: {os.path.basename(self._tsfile.path)}")
        except TsFileSaveError as exc:
            log.error("Could not save translation file", exc_info=True)
            self._set_status(f"Erro ao salvar: {exc}", error=True)

    def _do_lrelease(self):
        if not self._current or not self._current.get("ts"):
            QMessageBox.warning(self, "Sem .ts",
                "Rode o lupdate primeiro para gerar o .ts.")
            return
        if self._model.dirty: self._do_save()
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
        if not self._current or not self._tsfile:
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

        self._set_busy(True, "auto_tr")
        w = _AutoTranslateWorker(
            self._tsfile.entries,
            self._current["name"],
            self._current["code"],
            self._current.get("api_code", "E"),
            api_key,
            only_empty,
        )
        w.signals.done.connect(self._auto_translate_done)
        self._pool.start(w)

    @Slot(bool, str, str)
    def _auto_translate_done(self, ok: bool, message: str, json_results: str):
        self._set_busy(False, "auto_tr")
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
        for idx, entry in enumerate(self._tsfile.entries):
            if entry.source not in results:
                continue
            translated = results[entry.source]
            if entry.is_plural:
                if isinstance(translated, list) and any(s.strip() for s in translated):
                    self._model.update_entry(idx, [str(s).strip() for s in translated])
                    applied += 1
            else:
                if isinstance(translated, str) and translated.strip():
                    self._model.update_entry(idx, translated.strip())
                    applied += 1

        self._proxy.invalidateFilter()
        self._table.viewport().update()
        self._update_stats()

        has_empty = any(e.status in ("empty", "unfinished") for e in self._tsfile.entries)
        self._btn_auto_tr.setEnabled(has_empty)

        self._set_status(
            f"✦  {applied} string(s) preenchida(s) em {self._current['name']} — "
            "revise e clique em 💾 Salvar quando terminar."
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool, which: str):
        if which == "lu":
            self._btn_lu.setEnabled(not busy)
            self._btn_lu.setText("⏳  Rodando lupdate…" if busy
                                  else "↻  Extrair strings  (lupdate)")
        elif which == "lr":
            self._btn_lr.setEnabled(not busy)
            self._btn_lr.setText("⏳  Compilando…" if busy
                                  else "▶  Compilar .qm  (lrelease)")
        elif which == "auto_tr":
            self._btn_auto_tr.setEnabled(not busy)
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
                self._do_save(); ev.accept()
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
