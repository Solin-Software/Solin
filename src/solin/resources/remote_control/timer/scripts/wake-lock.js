export class ScreenWakeLock {
  #desired = false;
  #sentinel = null;
  #acquiring = false;

  constructor() {
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && this.#desired) {
        void this.#acquire();
      }
    });
  }

  setDesired(desired) {
    const next = Boolean(desired);
    if (next === this.#desired) return;
    this.#desired = next;
    if (this.#desired) {
      void this.#acquire();
    } else {
      void this.#release();
    }
  }

  async #acquire() {
    if (
      !this.#desired ||
      this.#acquiring ||
      this.#sentinel ||
      document.hidden ||
      !("wakeLock" in navigator)
    ) {
      return;
    }
    this.#acquiring = true;
    try {
      const sentinel = await navigator.wakeLock.request("screen");
      if (!this.#desired) {
        await sentinel.release();
        return;
      }
      this.#sentinel = sentinel;
      sentinel.addEventListener(
        "release",
        () => {
          if (this.#sentinel === sentinel) {
            this.#sentinel = null;
          }
        },
        { once: true },
      );
    } catch {
      // Browsers may decline wake lock on low battery; timer display continues normally.
    } finally {
      this.#acquiring = false;
    }
  }

  async #release() {
    const sentinel = this.#sentinel;
    this.#sentinel = null;
    if (!sentinel) return;
    try {
      await sentinel.release();
    } catch {
      // Ignore already-released sentinels.
    }
  }
}
