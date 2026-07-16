const MEDIA_LABELS = Object.freeze({
  video: "Vídeo",
  audio: "Áudio",
  image: "Imagem",
  document: "Documento",
  browser: "Navegador",
  announcement: "Anúncio",
  screen: "Tela",
  unknown: "Mídia",
});

const GROUP_LABELS = Object.freeze({
  section: "Seção",
  subsection: "Subseção",
  group: "Grupo",
});

export function formatDuration(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor((Number(milliseconds) || 0) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function mediaLabel(mediaKind) {
  return MEDIA_LABELS[mediaKind] ?? MEDIA_LABELS.unknown;
}

export function groupLabel(kind) {
  return GROUP_LABELS[kind] ?? "Seção";
}

export function collectionLabel(kind) {
  if (kind === "meeting") return "Reunião";
  if (kind === "linked_folder") return "Pasta vinculada";
  return "Playlist";
}

const WEEK_RANGE_FORMATTERS = new Map();
const DAY_MILLISECONDS = 24 * 60 * 60 * 1000;
const WEEK_MILLISECONDS = 7 * DAY_MILLISECONDS;

function localeKey(locale) {
  const requested = String(locale || "pt-BR");
  let key = "pt-BR";
  try {
    key = Intl.DateTimeFormat.supportedLocalesOf([requested])[0] || key;
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
  }
  return key;
}

function weekRangeFormatter(locale, includeYear) {
  const localeName = localeKey(locale);
  const key = `${localeName}:${includeYear ? "year" : "month"}`;
  if (!WEEK_RANGE_FORMATTERS.has(key)) {
    WEEK_RANGE_FORMATTERS.set(
      key,
      new Intl.DateTimeFormat(localeName, {
        day: "numeric",
        month: "long",
        ...(includeYear ? { year: "numeric" } : {}),
        timeZone: "UTC",
      }),
    );
  }
  return WEEK_RANGE_FORMATTERS.get(key);
}

function parseIsoDate(isoDate) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || ""));
  if (!match) return null;
  const [, yearText, monthText, dayText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const value = new Date(Date.UTC(year, month - 1, day));
  if (
    value.getUTCFullYear() !== year ||
    value.getUTCMonth() !== month - 1 ||
    value.getUTCDate() !== day
  ) {
    return null;
  }
  return value;
}

export function formatMeetingWeekRange(weekStart, locale) {
  const start = parseIsoDate(weekStart);
  if (!start) return "";
  const end = new Date(start.getTime() + 6 * DAY_MILLISECONDS);
  const formatter = weekRangeFormatter(
    locale,
    start.getUTCFullYear() !== end.getUTCFullYear(),
  );
  return typeof formatter.formatRange === "function"
    ? formatter.formatRange(start, end)
    : `${formatter.format(start)} – ${formatter.format(end)}`;
}

export function meetingWeekRelation(weekStart, currentWeekStart) {
  const week = parseIsoDate(weekStart);
  const current = parseIsoDate(currentWeekStart);
  if (!week || !current) return "";
  const offset = Math.round((week.getTime() - current.getTime()) / WEEK_MILLISECONDS);
  if (offset === 0) return "Esta semana";
  if (offset === 1) return "Próxima semana";
  if (offset === -1) return "Semana passada";
  if (offset > 1) return `Em ${offset} semanas`;
  return `Há ${Math.abs(offset)} semanas`;
}

export function meetingTypeLabel(meetingType) {
  const labels = {
    midweek: "Reunião do meio de semana",
    weekend: "Reunião do fim de semana",
    memorial: "Celebração",
    other: "Reunião",
  };
  return labels[meetingType] ?? labels.other;
}

export function meetingCollectionTitle(collection) {
  if (collection?.meetingType === "midweek") return "Vida e Ministério";
  if (collection?.meetingType === "weekend") {
    return collection.title || "Estudo de A Sentinela";
  }
  if (collection?.meetingType === "memorial") {
    return collection.title || "Celebração";
  }
  return collection?.title || "Reunião";
}

export function countMedia(nodes) {
  let count = 0;
  for (const node of nodes ?? []) {
    if (node.kind === "media") count += 1;
    count += countMedia(node.children);
  }
  return count;
}

export function findNode(nodes, nodeId) {
  for (const node of nodes ?? []) {
    if (node.id === nodeId) return node;
    const nested = findNode(node.children, nodeId);
    if (nested) return nested;
  }
  return null;
}

export function toneClass(value) {
  const text = String(value ?? "");
  const color = /^#([0-9a-f]{6})$/i.exec(text);
  if (color) {
    const red = Number.parseInt(color[1].slice(0, 2), 16) / 255;
    const green = Number.parseInt(color[1].slice(2, 4), 16) / 255;
    const blue = Number.parseInt(color[1].slice(4, 6), 16) / 255;
    const maximum = Math.max(red, green, blue);
    const minimum = Math.min(red, green, blue);
    const delta = maximum - minimum;
    let hue = 0;
    if (delta > 0 && maximum === red) hue = 60 * (((green - blue) / delta) % 6);
    else if (delta > 0 && maximum === green) hue = 60 * ((blue - red) / delta + 2);
    else if (delta > 0) hue = 60 * ((red - green) / delta + 4);
    const bucket = Math.round(((hue + 360) % 360) / 45) % 8;
    const toneByHue = [3, 4, 5, 5, 6, 0, 1, 2];
    return `tone-${toneByHue[bucket]}`;
  }
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
  }
  return `tone-${hash % 8}`;
}

export function thumbnailUrl(source, collectionId, node) {
  return mediaThumbnailUrl(source, collectionId, node?.id, node?.thumbnailId);
}

export function mediaThumbnailUrl(source, collectionId, nodeId, thumbnailId) {
  if (!thumbnailId || !source || !collectionId || !nodeId || source === "temporary") return null;
  const segments = [source, collectionId, nodeId].map((part) =>
    encodeURIComponent(String(part)),
  );
  return `/remote/api/thumbnails/${segments.join("/")}?v=${encodeURIComponent(thumbnailId)}`;
}

export function collectionThumbnailUrl(collection) {
  if (!collection?.thumbnailId || !collection.id || !collection.kind) return null;
  const segments = [collection.kind, collection.id].map((part) =>
    encodeURIComponent(String(part)),
  );
  return `/remote/api/collection-thumbnails/${segments.join("/")}?v=${encodeURIComponent(collection.thumbnailId)}`;
}
