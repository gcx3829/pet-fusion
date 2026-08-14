import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSearchEvents } from "../src/lib/events";
import type { SearchRecord } from "../src/types";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  readonly listeners = new Map<string, EventListener>();
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    this.listeners.set(type, listener);
  }

  removeEventListener(type: string) {
    this.listeners.delete(type);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, id: number) {
    const event = new MessageEvent<string>(type, {
      data: JSON.stringify({
        id,
        type,
        search_id: "search-01",
        created_at: "2026-08-14T00:00:00Z",
        payload: {},
      }),
      lastEventId: String(id),
    });
    this.listeners.get(type)?.(event);
    this.onmessage?.(event);
  }
}

function Probe({ search, reconnectKey }: { search: SearchRecord; reconnectKey: number }) {
  useSearchEvents(search, undefined, reconnectKey);
  return null;
}

const search: SearchRecord = {
  search_id: "search-01",
  status: "waiting_for_human",
  events_url: "/api/v1/searches/search-01/events",
};

describe("useSearchEvents", () => {
  afterEach(() => {
    MockEventSource.instances = [];
    vi.unstubAllGlobals();
  });

  it("resume 后从上次事件继续，而不是重放旧的 waiting 终止事件", () => {
    vi.stubGlobal("EventSource", MockEventSource);
    const view = render(<Probe search={search} reconnectKey={0} />);
    const first = MockEventSource.instances[0];
    first.emit("search.waiting_for_human", 7);

    view.rerender(<Probe search={{ ...search, status: "queued" }} reconnectKey={1} />);

    expect(MockEventSource.instances).toHaveLength(2);
    expect(MockEventSource.instances[1].url).toBe(
      "/api/v1/searches/search-01/events?after=7",
    );
  });
});
