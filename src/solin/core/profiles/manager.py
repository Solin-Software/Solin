"""
profile_manager.py — Solin
==========================
Gerencia perfis de usuário do Solin.

Cada perfil tem:
  - id único (slug normalizado, imutável)
  - nome de exibição
  - data de criação
  - configurações separadas (QSettings com organização própria)
  - dados separados (playlists, imagens, embedded)

Estrutura de dados
──────────────────
  DATA_DIR/
    profiles.json              ← registro global de perfis
    profiles/
      {slug}/
        playlists.json         ← playlists deste perfil
        meeting_trees.json     ← árvores customizadas das reuniões
        images/                ← imagens deste perfil
        embedded/              ← mídia recebida via Wi-Fi

QSettings por perfil
────────────────────
  Perfil ativo usa org "<base>_{slug}" em vez de "<base>".
  Em producao, <base> = "Solin"; em dev, <base> = "SolinDev".
  Isso garante isolamento entre perfis e tambem entre dev/producao.

Migração de dados legados
──────────────────────────
  Se existirem configuracoes no namespace base atual, mas nenhum perfil for
  encontrado, o app pergunta o nome do perfil e migra automaticamente os dados.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional, Self, cast

from PySide6.QtCore import QObject, QSettings, Signal

from solin.core.foundation.constants import (
    QSETTINGS_APP_APP,
    QSETTINGS_GLOBAL_APP,
    QSETTINGS_ORG_NAME,
    QSETTINGS_PREFS_APP,
    QSETTINGS_PROFILE_ORG_PREFIX,
    QSETTINGS_PROFILE_SCOPED_APPS,
)
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.storage.migration import (
    move_dir_if_exists as _move_dir_if_exists,
    move_file_if_exists as _move_file_if_exists,
)

log = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────
_PROFILES_FILENAME = "profiles.json"
_BASE_APP_GLOBAL_KEYS = {
    SettingsKey.INSTALL_ID,
    SettingsKey.PENDING_PATCH_CLEANUP,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_slug(name: str) -> str:
    """Converte nome legível em slug seguro para caminhos e QSettings."""
    slug = name.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    slug = slug.strip("_")
    return slug or "profile"


def _unique_slug(slug: str, existing: list[str]) -> str:
    """Garante unicidade adicionando sufixo numérico se necessário."""
    if slug not in existing:
        return slug
    i = 2
    while f"{slug}_{i}" in existing:
        i += 1
    return f"{slug}_{i}"


# ── ProfileInfo ───────────────────────────────────────────────────────────────

class ProfileInfo:
    """Metadados de um perfil. Imutáveis exceto pelo nome de exibição."""

    __slots__ = ("id", "name", "created_at")

    def __init__(self, id: str, name: str, created_at: float = 0.0):
        self.id         = id
        self.name       = name
        self.created_at = created_at or time.time()

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileInfo":
        return cls(d["id"], d["name"], d.get("created_at", time.time()))

    def __repr__(self) -> str:
        return f"ProfileInfo(id={self.id!r}, name={self.name!r})"


# ── ProfileManager ────────────────────────────────────────────────────────────

class ProfileManager(QObject):
    """
    Singleton responsável pelo ciclo de vida dos perfis.

    Signals
    -------
    profile_switched(profile_id: str)
        Emitido após a troca de perfil ativo, para que widgets possam
        recarregar dados específicos do perfil.
    """

    profile_switched = Signal(str)   # profile_id

    # ── Singleton ─────────────────────────────────────────────────────────
    _instance: Optional["ProfileManager"] = None

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        if cls._instance is None:
            obj = super().__new__(cls)
            obj._ready = False
            cls._instance = obj
        return cast(Self, cls._instance)

    def __init__(self, parent: Optional[QObject] = None):
        if self._ready:
            return
        super().__init__(parent)
        self._ready     = True
        self._data_dir  = ""
        self._active_id = ""
        self._profiles: list[ProfileInfo] = []

    # ── Inicialização ─────────────────────────────────────────────────────

    def init(self, data_dir: str) -> None:
        """
        Deve ser chamado UMA VEZ após paths.init(), antes de qualquer uso.
        """
        self._data_dir = data_dir
        self._load_profiles()
        log.debug("[ProfileManager] Initialized - %d profile(s)", len(self._profiles))

    # ── Leitura ───────────────────────────────────────────────────────────

    @property
    def profiles(self) -> list[ProfileInfo]:
        return list(self._profiles)

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def active_profile(self) -> Optional[ProfileInfo]:
        return next((p for p in self._profiles if p.id == self._active_id), None)

    def get_profile(self, profile_id: str) -> Optional[ProfileInfo]:
        return next((p for p in self._profiles if p.id == profile_id), None)

    def has_profiles(self) -> bool:
        return bool(self._profiles)

    def has_legacy_settings(self) -> bool:
        """True se existirem dados legados (sem perfil) no QSettings."""
        s = QSettings(QSETTINGS_ORG_NAME, QSETTINGS_PREFS_APP)
        return bool(s.allKeys())

    # ── QSettings com escopo de perfil ────────────────────────────────────

    def active_org(self) -> str:
        """Org do QSettings para o perfil ativo. Ex: 'Solin_default' ou 'SolinDev_default'."""
        return (
            f"{QSETTINGS_PROFILE_ORG_PREFIX}{self._active_id}"
            if self._active_id
            else QSETTINGS_ORG_NAME
        )

    def prefs(self, base: str = QSETTINGS_PREFS_APP) -> QSettings:
        """QSettings com namespace isolado do perfil ativo."""
        return QSettings(self.active_org(), base)

    def prefs_for(self, profile_id: str, base: str = QSETTINGS_PREFS_APP) -> QSettings:
        """QSettings para um perfil específico."""
        return QSettings(f"{QSETTINGS_PROFILE_ORG_PREFIX}{profile_id}", base)

    # ── Caminhos de dados do perfil ───────────────────────────────────────

    def paths_for(self, profile_id: str = "") -> ProfilePaths:
        pid = profile_id or self._active_id
        from solin.core.foundation import paths as _paths

        return ProfilePaths.from_roots(
            data_dir=self._data_dir,
            cache_dir=_paths.CACHE_DIR or None,
            profile_id=pid,
        )

    def profile_dir(self, profile_id: str = "") -> Path:
        return self.paths_for(profile_id).profile_dir

    def playlists_file(self, profile_id: str = "") -> str:
        return str(self.paths_for(profile_id).playlists_file)

    def meeting_trees_file(self, profile_id: str = "") -> str:
        return str(self.paths_for(profile_id).meeting_trees_file)

    def images_dir(self, profile_id: str = "") -> str:
        return str(self.paths_for(profile_id).images_dir)

    def embedded_dir(self, profile_id: str = "") -> str:
        return str(self.paths_for(profile_id).embedded_dir)

    def ensure_profile_dirs(self, profile_id: str = "") -> None:
        self.paths_for(profile_id).ensure_dirs()

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create_profile(self, name: str) -> ProfileInfo:
        existing = [p.id for p in self._profiles]
        slug = _unique_slug(_normalize_slug(name), existing)
        p = ProfileInfo(id=slug, name=name)
        self._profiles.append(p)
        self.ensure_profile_dirs(slug)
        self._save_profiles()
        log.info("[ProfileManager] Profile created: %r (%s)", name, slug)
        return p

    def rename_profile(self, profile_id: str, new_name: str) -> None:
        p = self.get_profile(profile_id)
        if p:
            p.name = new_name
            self._save_profiles()
            log.info("[ProfileManager] Profile renamed: %s -> %r", profile_id, new_name)

    def delete_profile(self, profile_id: str) -> bool:
        """
        Remove um perfil por completo.

        Apaga:
          - entrada em profiles.json
          - pasta DATA_DIR/profiles/<id> (playlists, imagens, embedded)
          - sessão NativeWebView do perfil
          - QSettings por perfil conhecidos

        Retorna False se for o único perfil ou se o ID não existir.
        """
        if len(self._profiles) <= 1:
            return False

        if not any(p.id == profile_id for p in self._profiles):
            return False

        was_active = profile_id == self._active_id
        self._profiles = [p for p in self._profiles if p.id != profile_id]
        self._save_profiles()

        # Remove dados persistentes do perfil.
        profile_paths = self.paths_for(profile_id)
        shutil.rmtree(profile_paths.profile_dir, ignore_errors=True)
        shutil.rmtree(profile_paths.native_webview_data_dir, ignore_errors=True)

        from solin.core.foundation import paths as _paths
        if _paths.CACHE_DIR:
            shutil.rmtree(Path(_paths.CACHE_DIR) / "profiles" / profile_id, ignore_errors=True)
            if profile_paths.native_webview_cache_dir is not None:
                shutil.rmtree(profile_paths.native_webview_cache_dir, ignore_errors=True)

        # Remove configs isoladas do perfil no QSettings.
        profile_org = f"{QSETTINGS_PROFILE_ORG_PREFIX}{profile_id}"
        for app_name in QSETTINGS_PROFILE_SCOPED_APPS:
            try:
                s = QSettings(profile_org, app_name)
                s.clear()
                s.sync()
            except Exception as exc:  # noqa: BLE001 - Qt settings backend cleanup boundary
                log.warning(
                    "[ProfileManager] Failed to clear QSettings %s/%s: %s",
                    profile_org, app_name, exc,
                )

        if was_active and self._profiles:
            self.set_active(self._profiles[0].id)
        else:
            gs = QSettings(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP)
            last = gs.value(SettingsKey.LAST_ACTIVE_PROFILE, "", str)
            if last == profile_id:
                gs.setValue(SettingsKey.LAST_ACTIVE_PROFILE, self._profiles[0].id)
                gs.sync()

        log.info("[ProfileManager] Profile removed: %s", profile_id)
        return True

    # ── Ativação ──────────────────────────────────────────────────────────

    def set_active(self, profile_id: str) -> None:
        """
        Ativa um perfil:
          1. Atualiza _active_id
          2. Garante que as pastas existam
          3. Redireciona paths.PLAYLISTS_FILE, IMAGES_DIR, EMBEDDED_DIR
          4. Persiste o perfil ativo no QSettings global
          5. Emite profile_switched
        """
        if not any(p.id == profile_id for p in self._profiles):
            raise ValueError(f"[ProfileManager] Perfil desconhecido: {profile_id!r}")

        self._active_id = profile_id
        self.ensure_profile_dirs()
        self._redirect_global_paths()

        # Atualiza org de QSettings para o perfil ativo
        from solin.core.profiles import settings as _ps
        _ps.set_org(self.active_org())

        # Persistir escolha
        gs = QSettings(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP)
        gs.setValue(SettingsKey.LAST_ACTIVE_PROFILE, profile_id)
        gs.sync()

        log.info("[ProfileManager] Active profile: %s", profile_id)
        self.profile_switched.emit(profile_id)

    def restore_last_active(self) -> Optional[str]:
        """
        Retorna o ID do último perfil ativo (se ainda existir),
        ou o primeiro da lista, ou None se não houver perfis.
        """
        if not self._profiles:
            return None
        gs = QSettings(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP)
        last = gs.value(SettingsKey.LAST_ACTIVE_PROFILE, "", str)
        if last and any(p.id == last for p in self._profiles):
            return last
        return self._profiles[0].id

    # ── Migração de dados legados ──────────────────────────────────────────

    def migrate_legacy(self, profile_name: str) -> ProfileInfo:
        """
        Cria um perfil a partir do estado legado (sem perfis).

        Move:
          - playlists.json
          - images/
          - embedded/
          - QSettings "<base>"/"ProjectionPrefs" → "<base>_{slug}"/"ProjectionPrefs"
          - QSettings "<base>"/"App"             → "<base>_{slug}"/"App"

        A limpeza do legado acontece somente para os caminhos/namespaces
        conhecidos acima, depois que os dados foram copiados/mesclados para o
        perfil novo. Isso evita que o app volte a detectar o estado antigo em
        execuções futuras.
        """
        profile = self.create_profile(profile_name)
        pid = profile.id

        # ── Playlists ──────────────────────────────────────────────────────
        src_pl = Path(self._data_dir) / "playlists.json"
        dst_pl = Path(self.playlists_file(pid))
        if _move_file_if_exists(src_pl, dst_pl):
            log.info("[Migration] playlists.json -> %s", dst_pl)

        # ── Images ────────────────────────────────────────────────────────
        src_img = Path(self._data_dir) / "images"
        dst_img = Path(self.images_dir(pid))
        if _move_dir_if_exists(src_img, dst_img):
            log.info("[Migration] images/ -> %s", dst_img)

        # ── Embedded ──────────────────────────────────────────────────────
        src_emb = Path(self._data_dir) / "embedded"
        dst_emb = Path(self.embedded_dir(pid))
        if _move_dir_if_exists(src_emb, dst_emb):
            log.info("[Migration] embedded/ -> %s", dst_emb)

        # ── QSettings ────────────────────────────────────────────────────
        for base in (QSETTINGS_PREFS_APP, QSETTINGS_APP_APP):
            src_s = QSettings(QSETTINGS_ORG_NAME, base)
            dst_s = QSettings(f"{QSETTINGS_PROFILE_ORG_PREFIX}{pid}", base)
            migrated_keys: list[str] = []
            for key in src_s.allKeys():
                if base == QSETTINGS_APP_APP and key in _BASE_APP_GLOBAL_KEYS:
                    continue
                dst_s.setValue(key, src_s.value(key))
                migrated_keys.append(key)
            dst_s.sync()
            if base == QSETTINGS_PREFS_APP:
                src_s.clear()
            else:
                for key in migrated_keys:
                    src_s.remove(key)
            src_s.sync()
            log.info(
                "[Migration] QSettings %s/%s -> %s%s/%s; legacy cleared",
                QSETTINGS_ORG_NAME, base, QSETTINGS_PROFILE_ORG_PREFIX, pid, base,
            )

        log.info("[Migration] Completed for profile %r (%s)", profile_name, pid)
        return profile

    # ── Internos ──────────────────────────────────────────────────────────

    def _profiles_file(self) -> Path:
        return Path(self._data_dir) / _PROFILES_FILENAME

    def _load_profiles(self) -> None:
        path = self._profiles_file()
        if not path.exists():
            self._profiles = []
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._profiles = [ProfileInfo.from_dict(d) for d in data.get("profiles", [])]
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            log.error("[ProfileManager] Failed to read profiles.json: %s", exc)
            self._profiles = []

    def _save_profiles(self) -> None:
        path = self._profiles_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"profiles": [p.to_dict() for p in self._profiles]}
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)   # atomic write

    def _redirect_global_paths(self) -> None:
        """Aponta os globals de paths.py para o diretório do perfil ativo."""
        from solin.core.foundation import paths
        paths.PLAYLISTS_FILE = self.playlists_file()
        paths.IMAGES_DIR     = self.images_dir()
        paths.EMBEDDED_DIR   = self.embedded_dir()
        # Garantir que as pastas existam
        Path(paths.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
        Path(paths.EMBEDDED_DIR).mkdir(parents=True, exist_ok=True)
        log.debug("[ProfileManager] paths → profile/%s", self._active_id)


# ── Acesso global (conveniência) ──────────────────────────────────────────────

def get() -> ProfileManager:
    """Retorna o singleton do ProfileManager (criando se necessário)."""
    return ProfileManager()
