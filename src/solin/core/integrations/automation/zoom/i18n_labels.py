"""Zoom desktop action prefixes from the Windows client's language resources.

To refresh, locate the Zoom installation's bin directory. English strings are
RT_STRING resources in zWinRes.dll; translations are UTF-8 LANGXML resources in
language/zLang_*.dll, with ID<number> elements and text attributes. Resource IDs
appear beside each list below.

Read DLLs as resource data with LoadLibraryExW, enumerate names/languages with
EnumResourceNamesW/EnumResourceLanguagesW, and retrieve bytes with FindResourceExW,
LoadResource and LockResource. Decode English's UTF-16 string-table entries or
parse the localized XML, then retain each unique action prefix. State descriptions
and shortcuts appended by Zoom are handled by the matcher, not stored as variants.
Button labels describe the action, which is the opposite of the current state.
"""

from __future__ import annotations


# Microphone muted: click to unmute. Resource ID: 23121.
AUDIO_MUTED_TEXT: list[str] = [
    "Unmute",
    "Ton ein",
    "Reactivar audio",
    "Rétablir le son",
    "Bunyikan",
    "Attiva audio",
    "ミュート解除",
    "음소거 해제",
    "Dempen opheffen",
    "Wyłącz wyciszenie",
    "Ativar som",
    "Включить звук",
    "Aktivera mikrofonen",
    "Sesi aç",
    "Bỏ tắt tiếng",
    "解除静音",
    "解除靜音",
]


# Microphone active: click to mute. Resource ID: 23120.
AUDIO_UNMUTED_TEXT: list[str] = [
    "Mute",
    "Ton aus",
    "Silenciar",
    "Couper le son",
    "Bisukan",
    "Disattiva audio",
    "ミュート",
    "음소거",
    "Dempen",
    "Wycisz",
    "Desativar som",
    "Выключить звук",
    "Inaktivera mikrofonen",
    "Sesi kapat",
    "Tắt tiếng",
    "静音",
    "靜音",
]


# Camera stopped: click to start. Resource ID: 23122.
VIDEO_STOPPED_TEXT: list[str] = [
    "Start my video",
    "Mein Video starten",
    "Iniciar mi vídeo",
    "Démarrer ma vidéo",
    "Mulai video saya",
    "avvia il mio video",
    "マイビデオを開始",
    "내 비디오 시작",
    "Mijn video starten",
    "Rozpocznij moje wideo",
    "Iniciar meu vídeo",
    "Включить мое видео",
    "Starta min video",
    "Videomu başlat",
    "Bắt đầu video của tôi",
    "开启视频",
    "開啟視訊",
]


# Camera active: click to stop. Resource ID: 23123.
VIDEO_STARTED_TEXT: list[str] = [
    "Stop my video",
    "Mein Video beenden",
    "Detener mi vídeo",
    "Arrêter ma vidéo",
    "Hentikan video saya",
    "Interrompi il mio video",
    "マイビデオを停止",
    "내 비디오 중지",
    "Mijn video stoppen",
    "Zatrzymaj moje wideo",
    "Interromper meu vídeo",
    "Остановить мое видео",
    "Stoppa min video",
    "Videomu durdur",
    "Dừng video của tôi",
    "停止视频",
    "停止視訊",
]


# Participants toolbar action. Resource ID: 23013.
PARTICIPANTS_TEXT: list[str] = [
    "Participants",
    "Teilnehmer",
    "Panel de participantes abierto",
    "participant·es",
    "Peserta",
    "Partecipanti",
    "参加者",
    "참가자",
    "Deelnemers",
    "Uczestnicy",
    "Participantes",
    "Участники",
    "Deltagare",
    "Katılımcılar",
    "Người tham gia",
    "参会者",
    "與會者",
]


# Audio disconnected: click to join. Resource ID: 22277.
CONNECT_AUDIO_TEXT: list[str] = [
    "Join audio",
    "An Audio teilnehmen",
    "Conectar audio",
    "Participer à l’audio",
    "Bergabung ke Audio",
    "Connettiti all’audio",
    "オーディオに接続",
    "오디오 연결",
    "Deelnemen aan audio",
    "Dołącz do poł. głos.",
    "Conectar áudio",
    "Присоединиться по аудио",
    "Delta via ljud",
    "Sese Katıl",
    "Kết nối âm thanh",
    "连接语音",
    "加入音訊",
]


# Computer-audio confirmation in zJoinAudioWndClass. Resource ID: 22294.
JOIN_AUDIO_TEXT: list[str] = [
    "Join with computer audio",
    "Mit Computeraudio teilnehmen",
    "Unirse con el audio compartido",
    "Participer par l’audio de l’ordinateur",
    "Bergabung dengan Audio Komputer",
    "Entra con l'audio del computer",
    "コンピュータオーディオで参加",
    "컴퓨터 오디오로 참가",
    "Deelnemen met computeraudio",
    "Dołącz z dźwiękiem z komputera",
    "Junte-se com o áudio do computador",
    "Войти с использованием звука компьютера",
    "Gå med via datorljud",
    "Bilgisayar Sesi ile Katıl",
    "Tham gia với âm thanh máy tính",
    "使用电脑音频加入",
    "使用電腦音訊加入",
]


# Leave computer audio from its menu. Resource ID: 22276.
LEAVE_AUDIO_TEXT: list[str] = [
    "Leave computer audio",
    "Das Computeraudio verlassen",
    "Dejar el audio de la computadora",
    "Quitter l’audio de l’ordinateur",
    "Putuskan audio komputer",
    "Esci da audio computer",
    "コンピュータオーディオを切断する",
    "컴퓨터 오디오 나가기",
    "Computeraudio verlaten",
    "Rozłącz dźwięk z komputera",
    "Sair com o áudio do computador",
    "Выключить звук компьютера",
    "Lämna datorljud",
    "Bilgisayar sesini bırak",
    "Rời khỏi âm thanh máy tính",
    "退出电脑音频",
    "停用電腦音訊",
]
