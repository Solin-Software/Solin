"""Zoom desktop control labels from the Windows client's language resources.

To refresh, locate the Zoom installation's bin directory. English strings are
RT_STRING resources in zWinRes.dll; translations are UTF-8 LANGXML resources in
language/zLang_*.dll, with ID<number> elements and text attributes. The IDs
below were checked against version 7.2.5.51704 and may move between releases.

Read DLLs as resource data with LoadLibraryExW, enumerate names/languages with
EnumResourceNamesW/EnumResourceLanguagesW, and retrieve bytes with FindResourceExW,
LoadResource and LockResource. Decode English's UTF-16 string-table entries or
parse the localized XML, then retain each unique label. For toolbar names that
include state or counts, retain the action prefix. A microphone or camera
label describes the action available in the current state.

When Zoom changes a label, verify the name exposed by Windows UI Automation.
Add the new name to the same list and retain verified names from older supported
Zoom versions. Each variant must identify the same control unambiguously within
its toolbar, menu, or dialog.
"""

from __future__ import annotations


# Microphone muted: click to unmute. Resource ID: 23123.
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


# Microphone active: click to mute. Resource ID: 23122.
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


# Camera stopped: click to start. Resource ID: 23124.
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


# Camera active: click to stop. Resource ID: 23125.
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


# Participants toolbar action and overflow title. Resource IDs: 22028, 23015–23018.
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


# More meeting controls on the toolbar. Resource ID: 22045.
TOOLBAR_OVERFLOW_TEXT: list[str] = [
    "More meeting controls",
    "Weitere Meetingsteuerungen",
    "Más controles de reunión",
    "Plus d'options de contrôle de la réunion",
    "Kontrol rapat lainnya",
    "Altri comandi della riunione",
    "その他のミーティング コントロール",
    "추가 회의 컨트롤",
    "Meer besturingselementen voor vergaderingen",
    "Więcej sterowników spotkania",
    "Mais controles da reunião",
    "Дополнительные элементы управления конференциями",
    "Mer möteskontroller",
    "Daha fazla toplantı kontrolü",
    "Nút điều khiển cuộc họp khác",
    "更多会议控件",
    "更多會議控制項",
]


# Position metadata appended to overflow grid item names. Resource ID: 12662.
GRID_POSITION_TEXT: list[str] = [
    "row @1, column @2",
    "Zeile @1, Spalte @2",
    "fila @1, columna @2",
    "ligne @1, colonne @2",
    "baris @1, kolom @2",
    "riga @1, colonna @2",
    "行@1、列@2",
    "@1행, @2열",
    "rij @1, kolom @2",
    "wiersz @1, kolumna @2",
    "linha @1, coluna @2",
    "строка @1, столбец @2",
    "rad @1, kolumn @2",
    "satır @1, sütun @2",
    "hàng @1, cột @2",
    "第@1行，第@2列",
    "第@1列，第@2欄",
]


# Participant list in the Participants panel. Resource ID: 23002.
PARTICIPANTS_LIST_TEXT: list[str] = [
    "Participant list, use arrow key to navigate",
    "Teilnehmerliste, verwenden Sie die Pfeiltasten zum Navigieren",
    "Lista de participantes, utilice las teclas de dirección para navegar",
    "Liste des participants, utilisez la touche flèche pour naviguer",
    "Daftar peserta, gunakan tombol panah untuk menavigasi",
    "Elenco di partecipanti, usa i tasti freccia per navigare",
    "参加者リスト: 移動するには矢印キーを使用します",
    "전화 참가자 목록, 화살표 키를 사용하여 탐색",
    "Lijst met deelnemers, gebruik de pijltoets om te navigeren",
    "Lista uczestników, użyj strzałek do nawigacji",
    "Lista de participantes, use a tecla de seta para navegar",
    "Список участников, для навигации используйте клавиши со стрелками",
    "Deltagarlista, använd piltangenterna för att navigera",
    "Katılımcı listesi, gezinmek için ok tuşunu kullanın",
    "Danh sách người tham gia, sử dụng phím mũi tên để điều hướng",
    "参会者列表，使用箭头键导航",
    "與會者清單，使用方向鍵瀏覽",
]


# Audio disconnected: click to join. Resource ID: 22279.
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


# Computer-audio confirmation in zJoinAudioWndClass. Resource ID: 22296.
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


# Leave computer audio from its menu. Resource ID: 22278.
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


# Audio settings toolbar menu. Resource IDs: 22473, 22255.
AUDIO_MENU_TEXT: list[str] = [
    "Audio settings",
    "Audio-Einstellungen",
    "Configuración del audio",
    "Paramètres audio",
    "Pengaturan Audio",
    "Impostazioni audio",
    "オーディオ設定",
    "오디오 설정",
    "Audio-instellingen",
    "Ustawienia dźwięku",
    "Configurações de Áudio",
    "Настройки звука",
    "Ljudinställningar",
    "Ses Ayarları",
    "Cài đặt âm thanh",
    "音频设置",
    "音訊設定",
    "Configuración de audio",
    "Настройки аудио",
]


# Mute all in the Participants panel. Resource ID: 22493.
MUTE_ALL_TEXT: list[str] = [
    "Mute all",
    "Alle stummschalten",
    "Silenciar a todos",
    "Désactiver tous les sons",
    "Bisukan Semua",
    "Disattiva audio per tutti",
    "全員をミュート",
    "모두 음소거",
    "Iedereen dempen",
    "Wycisz wszystkich",
    "Desativar Som de Todos",
    "Выключить звук для всех",
    "Inaktivera allas mikrofoner",
    "Tümünü Sessize Al",
    "Tắt tiếng Tất cả",
    "全体静音",
    "全體靜音",
]


# Affirmative button in the Mute all dialog. Resource ID: 2011.
CONFIRM_MUTE_ALL_TEXT: list[str] = [
    "Yes",
    "Ja",
    "Sí",
    "Oui",
    "Ya",
    "Sì",
    "はい",
    "예",
    "Tak",
    "Sim",
    "Да",
    "Evet",
    "Có",
    "是",
]


# Allow participants to unmute themselves in the Mute all dialog. Resource ID: 22484.
ALLOW_UNMUTE_TEXT: list[str] = [
    "Allow participants to unmute themselves",
    "Ermöglichen Sie es den Teilnehmern, selber ihre Stummschaltung aufzuheben",
    "Permitir que los participantes reactiven su audio ellos mismos",
    "Autoriser les participants à réactiver eux-mêmes leur micro",
    "Izinkan peserta untuk membunyikan mikrofon mereka sendiri",
    "Consenti ai partecipanti di attivare il proprio audio",
    "参加者に自分のミュートの解除を許可する",
    "참가자가 스스로 음소거를 해제할 수 있도록 허용",
    "Toestaan dat deelnemers hun audio inschakelen",
    "Zezwól uczestnikom na samodzielne wyłączanie wyciszenia",
    "Permitir que participantes ativem seu próprio som",
    "Разрешить участникам включать свой звук",
    "Tillåt deltagare att själva aktivera sina mikrofoner",
    "Katılımcıların kendi sesini açmasına izin ver",
    "Cho phép người tham gia tắt tiếng chính họ",
    "允许参会者自我解除静音",
    "允許與會者自行解除靜音",
]


# More options to manage all participants. Resource ID: 22043.
PARTICIPANTS_MORE_TEXT: list[str] = [
    "More options to manage all participants",
    "Weitere Optionen zum Verwalten aller Teilnehmer",
    "Más opciones para administrar todos los participantes",
    "Plus d'options pour gérer tous les participants",
    "Opsi lainnya untuk mengelola semua peserta",
    "Altre opzioni per gestire tutti i partecipanti",
    "すべての参加者を管理するためのその他のオプション",
    "모든 참가자 관리 추가 옵션",
    "Meer opties om alle deelnemers te beheren",
    "Więcej opcji do zarządzania wszystkimi uczestnikami",
    "Mais opções para gerenciar todos os participantes",
    "Дополнительные параметры для управления всеми участниками",
    "Fler alternativ för att hantera alla deltagare",
    "Tüm katılımcıları yönetmek için daha fazla seçenek",
    "Tùy chọn khác để quản lý tất cả người tham gia",
    "用于管理所有参会者的更多选项",
    "更多管理所有與會者的選項",
]


# Ask all to unmute in the Participants menu. Resource ID: 22707.
ASK_UNMUTE_TEXT: list[str] = [
    "Ask all to unmute",
    "Allg. Aufhebung der Stummschaltung anfordern",
    "Solicitar que todos reactiven el audio",
    "Demander à tout le monde la réactivation du micro",
    "Minta semua untuk membunyikan mikrofon",
    "Chiedi a tutti di attivare l’audio",
    "全員にミュート解除を求める",
    "모두에게 음소거 해제 요청",
    "Iedereen vragen om dempen op te heffen",
    "Poproś wszystkich o wyłączenie wyciszenia",
    "Pedir a todos para ativar o som",
    "Попросить всех включить звук",
    "Be alla att aktivera sina mikrofoner",
    "Herkesin Sesi Açmasını İste",
    "Yêu cầu tất cả bật tiếng",
    "要求所有人解除静音",
    "要求所有人解除靜音",
]
