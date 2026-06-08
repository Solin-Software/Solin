"""
media_api.py — Solin
═════════════════════════════════════════════════════════════════════════════
Cliente centralizado das APIs de mídia JW.org (cânticos e clipes musicais).

Suporte a línguas gestuais (Sign Language)
──────────────────────────────────────────
A API JW.org distingue dois conjuntos de cânticos:
  • sjjm  – "Symphonize Jehovah's Joy — with Music"  (vídeo com trilha Musical)
  • sjj   – "Symphonize Jehovah's Joy" (vídeo língua gestual, sem trilha separada)

A flag `is_sign_language: bool` deve ser derivada de
`JWLanguageService.is_media_sign_language` (que lê `isSignLanguage` da API JW)
e repassada a todas as funções deste módulo.

Clipes musicais (Original Songs — pub=osg)
──────────────────────────────────────────
Endpoint otimizado: GETPUBMEDIALINKS com pub=osg.
  • Idiomas normais : fileformat=MP3
  • Línguas gestuais: fileformat=MP4  (o clip é um vídeo em língua gestual)

Estrutura JSON do osg diferente do sjjm:
  • pubName está na raiz
  • files > {code} > MP3|MP4  → lista de itens
  • Cada item: title (nível do item), file > { url }, label (quando MP4)

Qualidade de vídeo
──────────────────
A resolução preferida e a direção de fallback são controladas pelas constantes
VIDEO_PREFERRED_QUALITY e VIDEO_QUALITY_FALLBACK_DIR em constants.py.
A função _pick_quality() é o único ponto de decisão de qualidade.

Fallback de idioma
──────────────────
Quando o idioma de mídia JW não tem conteúdo e cai no idioma da interface,
o fallback_is_sign SEMPRE é False — idiomas da interface nunca são gestuais.
Isso evita que, p. ex., o fallback "T" tente buscar sjj em vez de sjjm.
"""

from __future__ import annotations

import json
import os
import re
import time

import requests

from app.core.foundation import paths as _paths
from app.core.foundation.constants import (
    CACHE_TTL_DAYS,
    VIDEO_PREFERRED_QUALITY,
    VIDEO_QUALITY_FALLBACK_DIR,
    VIDEO_QUALITY_ORDER,
)

# ── Base URL comum ────────────────────────────────────────────────────────────

_JW_PUBMEDIA = (
    "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"
    "?output=json&pub={pub}&fileformat={fmt}&alllangs=0"
    "&langwritten={code}&txtCMSLang={code}"
)

# Endpoint mediador — usado para clipes de idiomas normais (exclui audio-descrição)
_JW_MEDIATOR_CLIPS = (
    "https://b.jw-cdn.org/apis/mediator/v1/categories/{code}/AudioOriginalSongs"
    "?detailed=1&clientType=www"
)

def song_publication_symbol(is_sign_language: bool) -> str:
    """
    Retorna o símbolo da publicação de cânticos correto:
      • True  → 'sjj'   (língua gestual — sem trilha musical)
      • False → 'sjjm'  (idioma normal — com trilha musical)
    Uso centralizado: toda referência programática a 'sjj'/'sjjm' deve passar aqui.
    """
    return "sjj" if is_sign_language else "sjjm"


def _songs_fmt(is_sign: bool, audio: bool) -> str:
    """
    Formato de arquivo para cânticos:
      • Língua gestual           → MP4  (independente de modo audio/vídeo)
      • Idioma normal, modo áudio → MP3
      • Idioma normal, modo vídeo → MP4
    """
    if is_sign:
        return "MP4"
    return "MP3" if audio else "MP4"


def _clips_fmt(is_sign: bool) -> str:
    """
    Formato de arquivo para clipes osg:
      • Língua gestual → MP4
      • Normal         → MP3
    """
    return "MP4" if is_sign else "MP3"


def _build_songs_url(api_code: str, is_sign: bool, audio: bool) -> str:
    pub = song_publication_symbol(is_sign)
    fmt = _songs_fmt(is_sign, audio)
    return _JW_PUBMEDIA.format(pub=pub, fmt=fmt, code=api_code)


def _build_clips_url(api_code: str, is_sign: bool) -> str:
    """URL do endpoint de clipes conforme o tipo de idioma."""
    if is_sign:
        # Língua gestual: GETPUBMEDIALINKS com pub=osg e MP4
        fmt = "MP4"
        return _JW_PUBMEDIA.format(pub="osg", fmt=fmt, code=api_code)
    else:
        # Idioma normal: endpoint mediador (exclui audio-descrição corretamente)
        return _JW_MEDIATOR_CLIPS.format(code=api_code)


def _pick_quality(
    items: list[dict],
    preferred: str = VIDEO_PREFERRED_QUALITY,
    fallback_dir: str = VIDEO_QUALITY_FALLBACK_DIR,
) -> str | None:
    """
    Escolhe o label de qualidade ideal dentre os itens disponíveis.

    Algoritmo (todos lendo VIDEO_PREFERRED_QUALITY / VIDEO_QUALITY_FALLBACK_DIR):
      1. Tenta o preferred exatamente.
      2. Determina as qualidades acima e abaixo do preferred em VIDEO_QUALITY_ORDER.
      3. fallback_dir='below' → tenta abaixo primeiro, depois acima (padrão conservador).
         fallback_dir='above' → tenta acima primeiro, depois abaixo.
      4. Se nenhuma qualidade conhecida existir, retorna qualquer label disponível
         (robusto a novas resoluções que a JW venha a lançar).
      5. Retorna None se não houver itens com label.
    """
    labels_set = {e.get("label") for e in items if isinstance(e, dict) and e.get("label")}
    if not labels_set:
        return None

    # 1. Preferência exata
    if preferred in labels_set:
        return preferred

    # 2. Posiciona preferred na ordem canônica
    order = list(VIDEO_QUALITY_ORDER)
    if preferred in order:
        idx = order.index(preferred)
        above = [q for q in order[:idx]    if q in labels_set]  # mais alto primeiro
        below = [q for q in order[idx+1:]  if q in labels_set]  # mais baixo primeiro
    else:
        above = [q for q in order if q in labels_set]
        below = []

    # 3. Ordena segundo fallback_dir
    ordered = (below + above) if fallback_dir != "above" else (above + below)

    if ordered:
        return ordered[0]

    # 4. Qualquer label disponível (resolução desconhecida)
    return next(iter(labels_set))


# ══════════════════════════════════════════════════════════════════════════════
# Cânticos — vídeo (MP4 / sjjm ou sjj)
# ══════════════════════════════════════════════════════════════════════════════

def _cache_path(api_code: str, is_sign: bool) -> str:
    os.makedirs(_paths.CACHE_DIR, exist_ok=True)
    suffix = "_sl" if is_sign else ""
    return os.path.join(_paths.CACHE_DIR, f"songs_{api_code}{suffix}.json")


def _is_cache_valid(api_code: str, is_sign: bool) -> bool:
    path = _cache_path(api_code, is_sign)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("songs"):
            return False
        age_days = (time.time() - data.get("_fetched_at", 0)) / 86400
        return age_days < CACHE_TTL_DAYS
    except Exception:
        return False


def _load_cache(api_code: str, is_sign: bool) -> tuple:
    try:
        with open(_cache_path(api_code, is_sign), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("songs"), data.get("pub_name", ""), data.get("_fetched_at", 0)
    except Exception:
        return None, "", 0


def _save_cache(api_code: str, is_sign: bool, songs: list, pub_name: str) -> None:
    path = _cache_path(api_code, is_sign)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"_fetched_at": time.time(), "pub_name": pub_name, "songs": songs},
            f, ensure_ascii=False, indent=2,
        )


def _parse_songs(data: dict, api_code: str, fmt: str) -> tuple[list, str]:
    """
    Parse da resposta GETPUBMEDIALINKS (sjjm ou sjj) em lista de cânticos.

    Usa _pick_quality() para selecionar a melhor resolução de vídeo conforme
    VIDEO_PREFERRED_QUALITY e VIDEO_QUALITY_FALLBACK_DIR em constants.py.
    Para áudio (MP3): sem filtragem de label; deduplica por número.
    Filtra versões com áudio descrição comparando parsed_num vs campo track.
    """
    pub_name = data.get("pubName", "")
    is_audio = fmt.upper() == "MP3"
    try:
        items = data["files"][api_code][fmt.upper()]
    except (KeyError, TypeError):
        return [], pub_name

    # Para vídeo: escolhe a melhor qualidade disponível de forma centralizada
    chosen_label: str | None = None
    if not is_audio:
        chosen_label = _pick_quality(items)

    songs: list[dict] = []
    seen_numbers: set[int] = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        if not is_audio:
            # Vídeo: exige a qualidade escolhida e sem legenda
            if item.get("label") != chosen_label:
                continue
            if item.get("subtitled") is not False:
                continue
        else:
            # Áudio: sem legenda, deduplica por número
            if item.get("subtitled") is not False:
                continue

        title = item.get("title", "").strip()
        match = re.match(r"^(\d+)(.*)$", title)
        if not match:
            continue

        num = int(match.group(1))

        # Filtra áudio-descrição: track oficial ≠ número do cântico (ex: 502 ≠ 2)
        track = int(item.get("track", 0))
        if track != num:
            continue

        if is_audio and num in seen_numbers:
            continue

        name = match.group(2).lstrip(".． \t-").strip()
        url = (item.get("file") or {}).get("url", "")
        if not url:
            continue

        duration = item.get("duration", 0) or 0
        songs.append({"number": num, "title": name, "url": url, "duration": duration})
        if is_audio:
            seen_numbers.add(num)

    return songs, pub_name


def fetch_songs(
    api_code: str,
    force: bool = False,
    fallback_code: str | None = None,
    is_sign_language: bool = False,
) -> tuple[list, str, float, bool]:
    """
    Busca cânticos JW (vídeo MP4).

    Parâmetros
    ----------
    api_code         : código JW do idioma de mídia (ex: 'T', 'ASL', 'BSL')
    force            : ignora cache
    fallback_code    : idioma da interface — usado como fallback quando api_code falha;
                       NUNCA é gestual (sempre sjjm).
    is_sign_language : se True, usa pub=sjj; caso contrário, pub=sjjm.

    Retorna (songs, pub_name, fetched_at_timestamp, from_cache).
    """
    if not force and _is_cache_valid(api_code, is_sign_language):
        songs, pub_name, fetched_at = _load_cache(api_code, is_sign_language)
        if songs is not None:
            return songs, pub_name, float(fetched_at), True

    url = _build_songs_url(api_code, is_sign_language, audio=False)
    fmt = _songs_fmt(is_sign_language, audio=False)

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception:
        # Fallback: idioma da interface — nunca é gestual
        if fallback_code and fallback_code != api_code:
            return fetch_songs(fallback_code, force=force, fallback_code=None,
                               is_sign_language=False)
        raise

    data = response.json()
    songs, pub_name = _parse_songs(data, api_code, fmt)

    if not songs and fallback_code and fallback_code != api_code:
        return fetch_songs(fallback_code, force=force, fallback_code=None,
                           is_sign_language=False)

    _save_cache(api_code, is_sign_language, songs, pub_name)
    return songs, pub_name, time.time(), False


# ══════════════════════════════════════════════════════════════════════════════
# Cânticos — áudio (MP3 ou MP4 para gestuais, sjjm ou sjj)
# ══════════════════════════════════════════════════════════════════════════════

def _songs_audio_cache_path(api_code: str, is_sign: bool) -> str:
    os.makedirs(_paths.CACHE_DIR, exist_ok=True)
    suffix = "_sl" if is_sign else ""
    return os.path.join(_paths.CACHE_DIR, f"songs_audio_{api_code}{suffix}.json")


def _is_songs_audio_cache_valid(api_code: str, is_sign: bool) -> bool:
    path = _songs_audio_cache_path(api_code, is_sign)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("songs"):
            return False
        age_days = (time.time() - data.get("_fetched_at", 0)) / 86400
        return age_days < CACHE_TTL_DAYS
    except Exception:
        return False


def _load_songs_audio_cache(api_code: str, is_sign: bool) -> tuple:
    try:
        with open(_songs_audio_cache_path(api_code, is_sign), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("songs"), data.get("pub_name", ""), data.get("_fetched_at", 0)
    except Exception:
        return None, "", 0


def _save_songs_audio_cache(api_code: str, is_sign: bool, songs: list, pub_name: str) -> None:
    path = _songs_audio_cache_path(api_code, is_sign)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"_fetched_at": time.time(), "pub_name": pub_name, "songs": songs},
            f, ensure_ascii=False, indent=2,
        )


def fetch_songs_audio(
    api_code: str,
    force: bool = False,
    fallback_code: str | None = None,
    is_sign_language: bool = False,
) -> tuple[list, str, float, bool]:
    """
    Busca cânticos JW em modo áudio.

    Para idiomas normais: pub=sjjm, fileformat=MP3.
    Para línguas gestuais: pub=sjj, fileformat=MP4
      (não há áudio separado — o "áudio" é o próprio vídeo em língua gestual).

    Parâmetros idênticos a fetch_songs.
    """
    if not force and _is_songs_audio_cache_valid(api_code, is_sign_language):
        songs, pub_name, fetched_at = _load_songs_audio_cache(api_code, is_sign_language)
        if songs is not None:
            return songs, pub_name, float(fetched_at), True

    url = _build_songs_url(api_code, is_sign_language, audio=True)
    fmt = _songs_fmt(is_sign_language, audio=True)

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception:
        if fallback_code and fallback_code != api_code:
            return fetch_songs_audio(fallback_code, force=force, fallback_code=None,
                                     is_sign_language=False)
        raise

    data = response.json()
    songs, pub_name = _parse_songs(data, api_code, fmt)

    if not songs and fallback_code and fallback_code != api_code:
        return fetch_songs_audio(fallback_code, force=force, fallback_code=None,
                                 is_sign_language=False)
    _save_songs_audio_cache(api_code, is_sign_language, songs, pub_name)
    return songs, pub_name, time.time(), False


# ══════════════════════════════════════════════════════════════════════════════
# Clipes musicais (Original Songs — pub=osg)
# ══════════════════════════════════════════════════════════════════════════════

def _clips_cache_path(api_code: str, is_sign: bool) -> str:
    os.makedirs(_paths.CACHE_DIR, exist_ok=True)
    suffix = "_sl" if is_sign else ""
    return os.path.join(_paths.CACHE_DIR, f"clips_{api_code}{suffix}.json")


def _is_clips_cache_valid(api_code: str, is_sign: bool) -> bool:
    path = _clips_cache_path(api_code, is_sign)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("clips"):
            return False
        age_days = (time.time() - data.get("_fetched_at", 0)) / 86400
        return age_days < CACHE_TTL_DAYS
    except Exception:
        return False


def _load_clips_cache(api_code: str, is_sign: bool) -> tuple:
    try:
        with open(_clips_cache_path(api_code, is_sign), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("clips"), data.get("_fetched_at", 0)
    except Exception:
        return None, 0


def _save_clips_cache(api_code: str, is_sign: bool, clips: list) -> None:
    path = _clips_cache_path(api_code, is_sign)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"_fetched_at": time.time(), "clips": clips},
            f, ensure_ascii=False, indent=2,
        )


def _parse_clips_mediator(data: dict) -> list:
    """
    Parser para o endpoint mediador (/mediator/v1/categories/{code}/AudioOriginalSongs).

    Usado para idiomas normais (não gestuais). O mediador já retorna apenas clipes
    sem áudio-descrição quando filtramos subtitled=False.

    Estrutura:
      data["category"]["media"]  → lista de itens
      item["title"]              → "A melhor vida"
      item["duration"]           → 251.884
      item["files"][]            → { "progressiveDownloadURL": "...", "subtitled": bool }
    """
    clips: list[dict] = []
    try:
        media_items = data["category"]["media"]
    except (KeyError, TypeError):
        return clips

    for item in media_items:
        if not isinstance(item, dict):
            continue
        title    = item.get("title", "").strip()
        duration = item.get("duration", 0) or 0
        url = ""
        for f in item.get("files", []):
            if not isinstance(f, dict):
                continue
            if f.get("subtitled") is not False:
                continue
            candidate = f.get("progressiveDownloadURL", "")
            if candidate:
                url = candidate
                break
        if title and url:
            clips.append({"title": title, "url": url, "duration": duration})

    return clips


def _parse_clips_osg(data: dict, api_code: str) -> list:
    """
    Parser para o endpoint GETPUBMEDIALINKS (pub=osg, fileformat=MP4).

    Usado para línguas gestuais. Seleciona a melhor resolução via _pick_quality()
    e **inverte** a lista — o osg retorna do mais antigo para o mais novo, então
    revertemos para que os lançamentos mais recentes apareçam primeiro.

    Estrutura:
      data["files"][api_code]["MP4"]  → lista de itens
      item["title"]                   → "A melhor vida"
      item["label"]                   → "720p"
      item["file"]["url"]             → URL do MP4
    """
    clips: list[dict] = []
    try:
        items = data["files"][api_code]["MP4"]
    except (KeyError, TypeError):
        return clips

    chosen_label = _pick_quality(items)

    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("label") != chosen_label:
            continue
        title = item.get("title", "").strip()
        if not title:
            continue
        url = (item.get("file") or {}).get("url", "")
        if not url:
            continue
        duration = item.get("duration", 0) or 0
        clips.append({"title": title, "url": url, "duration": duration})

    # Inverte: osg ordena do mais antigo ao mais novo → queremos o mais novo primeiro
    clips.reverse()
    return clips


def fetch_clips(
    api_code: str,
    force: bool = False,
    fallback_code: str | None = None,
    is_sign_language: bool = False,
) -> tuple[list, float, bool]:
    """
    Busca clipes musicais originais (Original Songs).

    • Idiomas normais  → endpoint mediador (/mediator/…/AudioOriginalSongs)
                         Exclui áudio-descrição via subtitled=False.
    • Línguas gestuais → endpoint osg (GETPUBMEDIALINKS, pub=osg, fileformat=MP4)
                         Melhor resolução via _pick_quality(); resultado invertido
                         (mais novo primeiro, pois osg retorna do mais antigo ao mais novo).

    Fallback para idioma da interface sempre com is_sign_language=False.
    Retorna (clips, fetched_at_timestamp, from_cache).
    """
    if not force and _is_clips_cache_valid(api_code, is_sign_language):
        clips, fetched_at = _load_clips_cache(api_code, is_sign_language)
        if clips is not None:
            return clips, float(fetched_at), True

    url = _build_clips_url(api_code, is_sign_language)

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception:
        if fallback_code and fallback_code != api_code:
            return fetch_clips(fallback_code, force=force, fallback_code=None,
                               is_sign_language=False)
        raise

    data = response.json()

    if is_sign_language:
        clips = _parse_clips_osg(data, api_code)
    else:
        clips = _parse_clips_mediator(data)

    if not clips and fallback_code and fallback_code != api_code:
        return fetch_clips(fallback_code, force=force, fallback_code=None,
                           is_sign_language=False)

    _save_clips_cache(api_code, is_sign_language, clips)
    return clips, time.time(), False


# ══════════════════════════════════════════════════════════════════════════════
# Utilitários de cache (usados por settings_widget, etc.)
# ══════════════════════════════════════════════════════════════════════════════

def get_cache_date(api_code: str, is_sign_language: bool = False) -> float | None:
    """Retorna o timestamp do cache de cânticos (vídeo) se existir."""
    try:
        with open(_cache_path(api_code, is_sign_language), "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("_fetched_at", 0)
        return float(ts) if ts else None
    except Exception:
        return None
