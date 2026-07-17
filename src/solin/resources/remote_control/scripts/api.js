import { t } from "./i18n.js";

const API_ROOT = "/remote/api";
const REQUEST_TIMEOUT_MS = 15_000;
const HEARTBEAT_TIMEOUT_MS = 55_000;
const RECONNECT_MAX_MS = 30_000;

export class ApiError extends Error {
  constructor(message, { status = 0, code = "failed", retryable = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

export class RemoteApi {
  #csrfToken = null;

  clearSecrets() {
    this.#csrfToken = null;
  }

  async login(username, password) {
    const response = await this.#request("/auth/login", {
      method: "POST",
      body: { username, password },
      csrf: false,
      clientMode: currentDisplayMode(),
    });
    this.#captureCsrf(response);
    return response;
  }

  async setup() {
    return this.#request("/setup", { csrf: false });
  }

  async logout() {
    try {
      return await this.#request("/logout", { method: "POST" });
    } finally {
      this.clearSecrets();
    }
  }

  async bootstrap() {
    const response = await this.#request("/bootstrap");
    this.#captureCsrf(response);
    return response;
  }

  async catalog() {
    return this.#request("/catalog");
  }

  async command(command) {
    return this.#request("/commands", { method: "POST", body: command });
  }

  #captureCsrf(response) {
    if (typeof response?.csrfToken === "string" && response.csrfToken.length > 0) {
      this.#csrfToken = response.csrfToken;
    }
  }

  async #request(path, { method = "GET", body, csrf = true, clientMode = "" } = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const headers = { Accept: "application/json" };

    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (csrf && method !== "GET" && this.#csrfToken) {
      headers["X-CSRF-Token"] = this.#csrfToken;
    }
    if (clientMode) {
      headers["X-Solin-Display-Mode"] = clientMode;
    }

    try {
      const response = await fetch(`${API_ROOT}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        signal: controller.signal,
      });

      const payload = await readJson(response);
      if (!response.ok) {
        const error = payload?.error ?? {};
        throw new ApiError(statusMessage(response.status), {
          status: response.status,
          code: error.code || statusCode(response.status),
          retryable: Boolean(error.retryable),
        });
      }
      return payload;
    } catch (error) {
      if (error instanceof ApiError) {
        throw error;
      }
      const timedOut = error instanceof DOMException && error.name === "AbortError";
      throw new ApiError(
        timedOut
          ? t("api.timeout")
          : t("api.unreachable"),
        { code: timedOut ? "timeout" : "network", retryable: true },
      );
    } finally {
      window.clearTimeout(timeout);
    }
  }
}

function currentDisplayMode() {
  const standalone = window.matchMedia?.("(display-mode: standalone)").matches
    || window.navigator.standalone === true;
  return standalone ? "standalone" : "browser";
}

async function readJson(response) {
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    if (response.status === 204) {
      return {};
    }
    if (!response.ok) {
      const message = (await response.text()).trim();
      return {
        error: {
          code: statusCode(response.status),
          message: message || statusMessage(response.status),
          retryable: response.status === 429 || response.status >= 500,
        },
      };
    }
    throw new ApiError(t("api.invalidResponse"), {
      status: response.status,
      code: "invalid_response",
    });
  }
  try {
    return await response.json();
  } catch {
    throw new ApiError(t("api.incompleteResponse"), {
      status: response.status,
      code: "invalid_response",
    });
  }
}

function statusCode(status) {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 429) return "rate_limited";
  return "failed";
}

function statusMessage(status) {
  if (status === 401) return t("api.unauthorized");
  if (status === 403) return t("api.forbidden");
  if (status === 429) return t("api.rateLimited");
  return t("api.requestFailed");
}

export class EventStream {
  #closed = false;
  #generation = 0;
  #heartbeatTimer = 0;
  #reconnectTimer = 0;
  #retry = 0;
  #socket = null;

  constructor({ onEvent, onStatus }) {
    this.onEvent = onEvent;
    this.onStatus = onStatus;
  }

  connect({ immediate = false } = {}) {
    this.#closed = false;
    window.clearTimeout(this.#reconnectTimer);
    this.#teardownSocket();
    if (immediate) {
      this.#retry = 0;
    }
    this.#open(++this.#generation);
  }

  reconnectNow() {
    this.#teardownSocket();
    this.connect({ immediate: true });
  }

  close() {
    this.#closed = true;
    this.#generation += 1;
    window.clearTimeout(this.#reconnectTimer);
    window.clearTimeout(this.#heartbeatTimer);
    this.#teardownSocket();
  }

  #open(generation) {
    if (this.#closed) return;
    this.onStatus(this.#retry === 0 ? "connecting" : "reconnecting");

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}${API_ROOT}/ws`);
    let opened = false;
    this.#socket = socket;

    socket.addEventListener("open", () => {
      if (generation !== this.#generation) return;
      opened = true;
      this.#retry = 0;
      this.onStatus("syncing");
      this.#armHeartbeat(generation);
    });

    socket.addEventListener("message", (event) => {
      if (generation !== this.#generation) return;
      this.#armHeartbeat(generation);
      try {
        const message = JSON.parse(event.data);
        if (message?.type !== "heartbeat") {
          this.onEvent(message);
        }
      } catch {
        socket.close(1002, "Invalid event payload");
      }
    });

    socket.addEventListener("close", (event) => {
      if (generation !== this.#generation || this.#closed) return;
      window.clearTimeout(this.#heartbeatTimer);
      if (event.code === 4401 || event.code === 4403) {
        this.onStatus("unauthorized");
        return;
      }
      this.#scheduleReconnect(generation);
      if (!opened) {
        this.onStatus("authentication-check");
      }
    });

    socket.addEventListener("error", () => {
      if (generation === this.#generation) {
        socket.close();
      }
    });
  }

  #armHeartbeat(generation) {
    window.clearTimeout(this.#heartbeatTimer);
    this.#heartbeatTimer = window.setTimeout(() => {
      if (generation === this.#generation) {
        this.#socket?.close(4000, "Heartbeat timeout");
      }
    }, HEARTBEAT_TIMEOUT_MS);
  }

  #scheduleReconnect(generation) {
    this.#retry += 1;
    this.onStatus(navigator.onLine ? "reconnecting" : "offline");
    const exponential = Math.min(RECONNECT_MAX_MS, 750 * 2 ** (this.#retry - 1));
    const jitter = exponential * (0.8 + Math.random() * 0.4);
    this.#reconnectTimer = window.setTimeout(() => {
      if (generation === this.#generation && !this.#closed) {
        this.#open(++this.#generation);
      }
    }, jitter);
  }

  #teardownSocket() {
    const socket = this.#socket;
    this.#socket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) {
      socket.close(1000, "Client reconnect");
    }
  }
}

export function newCommandId() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
