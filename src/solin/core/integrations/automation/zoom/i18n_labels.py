from __future__ import annotations

# ─────────────────────────────────────────────────────────────────
#  Configuracao de idioma (PT-BR)
#  Altere aqui se o Zoom estiver em outro idioma ou versao diferente.
#
#  LOGICA: o texto do botao descreve a ACAO que vai ocorrer ao clicar,
#          que e o OPOSTO do estado atual.
#  Exemplo: botao "Desativar som" significa audio ESTA ativo (unmuted)
#           botao "Ativar som"   significa audio ESTA mudo  (muted)
# ─────────────────────────────────────────────────────────────────

# Texto do botao quando audio JA ESTA MUDO (clicar desmuta)
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

# Texto do botao quando audio ESTA ATIVO (clicar muta)
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

# Texto do botao quando video JA ESTA PARADO (clicar inicia)
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

# Texto do botao quando video JA ESTA ATIVO (clicar para)
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

# Texto do btn_muteAudio quando audio esta DESCONECTADO (sem audio nenhum)
CONNECT_AUDIO_TEXT: list[str] = [
    "conectar áudio",
    "conectar audio",
    "entrar com o áudio",
    "entrar com o audio",
    "connect audio",    # fallback EN
    "join audio",
]

# Texto do botao dentro do popup "Entrar com Audio" (zJoinAudioWndClass)
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

# ── Itens de menu SEM controlID (MenuItems genericos do Windows) ─
# Texto e a unica alternativa para identifica-los.
_LEAVE_AUDIO_PATTERNS: list[str] = [
    "sair do áudio",
    "sair do audio",
    "sair com o áudio",
    "leave computer audio",
    "leave audio",
]

# ── Separadores para contagem de pessoas em nomes de participantes ─
# Ordem de prioridade: o PRIMEIRO separador encontrado no nome é usado.
# Recomendado usar " & " nos nomes do Zoom (ex: "Felipe & Júlia").
PEOPLE_SEPARATORS: list[str] = [" & ", " | ", " + "]
