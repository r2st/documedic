import '@testing-library/jest-dom/vitest';

// Node's own experimental global `localStorage` (added in Node 22+, gated behind
// --localstorage-file) shadows jsdom's implementation under this Node/vitest/jsdom
// combination, leaving `window.localStorage` undefined instead of a working Storage. Install a
// minimal, spec-shaped in-memory polyfill so lib/api.ts's localStorage calls work in tests.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }

  clear(): void {
    this.store.clear();
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null;
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

const memoryStorage = new MemoryStorage();
for (const target of [globalThis, window] as const) {
  Object.defineProperty(target, 'localStorage', {
    value: memoryStorage,
    configurable: true,
    writable: true,
  });
}
