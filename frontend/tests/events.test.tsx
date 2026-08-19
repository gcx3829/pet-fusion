import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { derivePromptRefinementState, useSearchEvents } from "../src/lib/events";
import type { SearchEvent, SearchRecord } from "../src/types";

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
  const { events } = useSearchEvents(search, undefined, reconnectKey);
  return <output data-testid="event-count">{events.length}</output>;
}

function TypeProbe({ search }: { search: SearchRecord }) {
  const { events } = useSearchEvents(search);
  return <output data-testid="event-type">{events.at(-1)?.type ?? ""}</output>;
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

    MockEventSource.instances[1].emit("search.waiting_for_human", 7);
    expect(screen.getByTestId("event-count")).toHaveTextContent("1");
  });

  it("接收 prompt refiner 状态事件，但由上层决定如何展示完整版本", () => {
    vi.stubGlobal("EventSource", MockEventSource);
    render(<TypeProbe search={search} />);
    act(() => MockEventSource.instances[0].emit("prompt.refiner.started", 8));

    expect(screen.getByTestId("event-type")).toHaveTextContent("prompt.refiner.started");
  });

  it("乱序重放 started 不会把同轮 ready 回退为永久处理中", () => {
    const events: SearchEvent[] = [
      { id: "12", type: "prompt.refiner.ready", data: { round_index: 1, mode: "revision" } },
      { id: "11", type: "prompt.refiner.started", data: { round_index: 1, mode: "revision" } },
    ];

    expect(derivePromptRefinementState(events)).toMatchObject({
      status: "ready",
      roundIndex: 1,
      mode: "revision",
    });
  });

  it("新一轮 started 优先于上一轮 ready，普通搜索失败不篡改 Prompt 成功状态", () => {
    const priorReady: SearchEvent = {
      id: "12",
      type: "prompt.refiner.ready",
      data: { round_index: 1, refinement_mode: "revision" },
    };
    const nextStarted: SearchEvent = {
      id: "13",
      type: "prompt.refiner.started",
      data: { round_index: 2 },
    };

    expect(derivePromptRefinementState([nextStarted, priorReady])).toMatchObject({
      status: "started",
      roundIndex: 2,
    });
    expect(derivePromptRefinementState([priorReady])).toMatchObject({
      status: "ready",
      mode: "revision",
    });
    expect(derivePromptRefinementState([priorReady], 2)).toEqual({ status: "idle" });
  });
});
