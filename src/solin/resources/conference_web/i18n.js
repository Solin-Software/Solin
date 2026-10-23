// Strings for the attendee and stage pages, chosen from the browser's languages.
(function () {
  "use strict";
  const MESSAGES = {
    en: {
      title: "Congregation meeting",
      notOpen: "The meeting has not started yet. This page will update by itself.",
      badLink: "This link is incomplete. Open the whole link you received.",
      name: "Your name or family",
      namePlaceholder: "e.g. The Smith family",
      attendance: "How many people are watching with you (including you)?",
      fewer: "fewer",
      more: "more",
      join: "Join the meeting",
      rejoin: "Join again",
      joining: "Joining…",
      joinFailed: "Could not join. Check your connection and try again.",
      removed: "You were removed from this meeting.",
      waiting: "Waiting for the attendant to let you in…",
      reconnecting: "Reconnecting…",
      raiseHand: "✋ Raise hand",
      lowerHand: "✋ Hand raised (tap to lower)",
      speakNow: "🎙 You can speak now",
      unmute: "🎙 Turn microphone on",
      mute: "🎙 Microphone on (tap to turn off)",
      micBlocked: "Allow the microphone in your browser to comment.",
      cameraOn: "📷 Turn camera on",
      cameraOff: "📷 Camera on (tap to turn off)",
      cameraBlocked: "Allow the camera in your browser to be shown in the hall.",
      shownInHall: "📷 You are shown in the hall now",
      leave: "Leave",
      left: "You left the meeting.",
      stageTitle: "Raised hands",
      stageNone: "No hands raised",
      stageClosed: "The meeting is not running.",
      stageSpeaking: "speaking",
      stageWatching: "{n} watching remotely",
    },
    pt: {
      title: "Reunião da congregação",
      notOpen: "A reunião ainda não começou. Esta página se atualiza sozinha.",
      badLink: "Este link está incompleto. Abra o link inteiro que você recebeu.",
      name: "Seu nome ou família",
      namePlaceholder: "Ex.: Família Silva",
      attendance: "Quantas pessoas estão assistindo com você (incluindo você)?",
      fewer: "menos",
      more: "mais",
      join: "Entrar na reunião",
      rejoin: "Entrar novamente",
      joining: "Entrando…",
      joinFailed: "Não foi possível entrar. Verifique a conexão e tente de novo.",
      removed: "Você foi removido desta reunião.",
      waiting: "Aguardando o indicador deixar você entrar…",
      reconnecting: "Reconectando…",
      raiseHand: "✋ Levantar a mão",
      lowerHand: "✋ Mão levantada (toque para baixar)",
      speakNow: "🎙 Você já pode falar",
      unmute: "🎙 Ligar o microfone",
      mute: "🎙 Microfone ligado (toque para desligar)",
      micBlocked: "Permita o microfone no navegador para comentar.",
      cameraOn: "📷 Ligar a câmera",
      cameraOff: "📷 Câmera ligada (toque para desligar)",
      cameraBlocked: "Permita a câmera no navegador para aparecer no salão.",
      shownInHall: "📷 Você está aparecendo no salão agora",
      leave: "Sair",
      left: "Você saiu da reunião.",
      stageTitle: "Mãos levantadas",
      stageNone: "Nenhuma mão levantada",
      stageClosed: "A reunião não está acontecendo.",
      stageSpeaking: "falando",
      stageWatching: "{n} assistindo a distância",
    },
  };
  const language = (navigator.languages || [navigator.language || "en"])
    .map((code) => String(code).toLowerCase().split("-")[0])
    .find((code) => code in MESSAGES) || "en";
  const strings = MESSAGES[language];
  window.SolinText = {
    language,
    t(key, values) {
      let text = strings[key] || MESSAGES.en[key] || key;
      for (const [name, value] of Object.entries(values || {})) text = text.replace("{" + name + "}", value);
      return text;
    },
    apply(root) {
      document.documentElement.lang = language;
      for (const element of (root || document).querySelectorAll("[data-t]")) element.textContent = this.t(element.dataset.t);
      for (const element of (root || document).querySelectorAll("[data-t-placeholder]")) element.placeholder = this.t(element.dataset.tPlaceholder);
      for (const element of (root || document).querySelectorAll("[data-t-label]")) element.setAttribute("aria-label", this.t(element.dataset.tLabel));
    },
  };
})();
