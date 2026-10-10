from __future__ import annotations

# ─────────────────────────────────────────────────────────────────
#  Language configuration (PT-BR)
# Adjust these labels if Zoom uses a different language or version.
#
# LOGIC: button text describes the ACTION performed on click,
# which is the OPPOSITE of the current state.
#  Example: button "Desativar som" means audio IS active (unmuted).
#           Button "Ativar som" means audio IS muted.
# ─────────────────────────────────────────────────────────────────

# Button text when audio IS ALREADY MUTED (click to unmute)
AUDIO_MUTED_TEXT: list[str] = [
    "ativar som",
    "ativar áudio",
    "ativar audio",
    "unmute",
    "解除静音，当前已静音",
    "解除靜音，目前已靜音",
    "ミュート解除、現在ミュート中",
    "reactivar audio",
    "rétablir le son",
    "ton ein",
    "включить звук",
    "음소거 해제",
    "attiva audio",
    "bỏ tắt tiếng",
    "sesi aç",
    "wyłącz wyciszenie",
    "bunyikan",
    "dempen opheffen",
    "aktivera mikrofonen – mikrofonen är för närvarande inaktiverad",
]

# Button text when audio IS ACTIVE (click to mute)
AUDIO_UNMUTED_TEXT: list[str] = [
    "desativar som",
    "desativar áudio",
    "desativar audio",
    "mute",
    "静音，当前已解除静音",
    "靜音，目前已解除靜音",
    "ミュート、現在ミュート解除中",
    "silenciar",
    "couper le son",
    "ton aus",
    "выключить звук",
    "음소거",
    "disattiva audio",
    "tắt tiếng",
    "sesi kapat",
    "wycisz",
    "bisukan",
    "dempen",
    "inaktivera mikrofonen – mikrofonen är för närvarande aktiverad",
]

# Button text when video IS ALREADY STOPPED (click to start)
VIDEO_STOPPED_TEXT: list[str] = [
    "iniciar vídeo",
    "iniciar video",
    "iniciar meu vídeo",
    "iniciar meu video",
    "câmera desativada",
    "camera desativada",
    "start video",
    "start my video",
    "开启视频",
    "開啟視訊",
    "マイビデオを開始",
    "iniciar mi vídeo",
    "démarrer ma vidéo",
    "mein video starten",
    "включить мое видео",
    "내 비디오 시작",
    "avvia il mio video",
    "bắt đầu video của tôi",
    "videomu başlat",
    "rozpocznij moje wideo",
    "mulai video saya",
    "mijn video starten",
    "starta min video",
]

# Button text when video IS ALREADY ACTIVE (click to stop)
VIDEO_STARTED_TEXT: list[str] = [
    "interromper vídeo",
    "interromper video",
    "interromper meu vídeo",
    "interromper meu video",
    "parar vídeo",
    "parar video",
    "stop video",
    "stop my video",
    "停止视频",
    "停止視訊",
    "マイビデオを停止",
    "detener mi vídeo",
    "arrêter ma vidéo",
    "mein video beenden",
    "остановить мое видео",
    "내 비디오 중지",
    "interrompi il mio video",
    "dừng video của tôi",
    "videomu durdur",
    "zatrzymaj moje wideo",
    "hentikan video saya",
    "mijn video stoppen",
    "stoppa min video",
]

PARTICIPANTS_TEXT: list[str] = [
    "participantes",
    "participants",
    "参会者",
    "與會者",
    "参加者",
    "participant·es",
    "teilnehmer",
    "участники",
    "참가자",
    "partecipanti",
    "người tham gia",
    "katılımcılar",
    "uczestnicy",
    "peserta",
    "deelnemers",
    "deltagare",
]

# btn_muteAudio text when audio is DISCONNECTED (no audio at all)
CONNECT_AUDIO_TEXT: list[str] = [
    "conectar áudio",
    "conectar audio",
    "entrar com o áudio",
    "entrar com o audio",
    "connect audio",    # fallback EN
    "join audio",
]

# Button text inside the "Join Audio" popup (zJoinAudioWndClass)
_JOIN_AUDIO_BTN_PATTERNS: list[str] = [
    "junte-se com o áudio do computador",
    "junte-se com o audio do computador",
    "junte-se com o áudio",
    "junte-se com o audio",
    "entrar com áudio do computador",
    "entrar com audio do computador",
    "join with computer audio",
    "join computer audio",
]

# Menu items WITHOUT controlID (generic Windows MenuItems)
# Text is the only alternative for identifying them.
_LEAVE_AUDIO_PATTERNS: list[str] = [
    "sair do áudio",
    "sair do audio",
    "sair com o áudio",
    "leave computer audio",
    "leave audio",
]

# Separators for counting people in participant names
# Priority order: use the FIRST separator found in the name.
# Use " & " in Zoom names (e.g. "Felipe & Júlia").
PEOPLE_SEPARATORS: list[str] = [" & ", " | ", " + "]
