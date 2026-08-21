import { useEffect, useRef, useState } from "react";
import type {
  PromptGenerationMode,
  PromptRefinementEventState,
  PromptRefinementMode,
  SearchEvent,
  SearchRecord,
} from "../types";
import { searchEventsUrl } from "./api";

export type EventConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "settled"
  | "unavailable";

const EVENT_TYPES = [
  "search.queued",
  "search.started",
  "round.queued",
  "round.generation.started",
  "round.candidate.ready",
  "round.critic.started",
  "round.evaluation.ready",
  "round.winner.updated",
  "search.global_winner.updated",
  "prompt.refiner.started",
  "prompt.refiner.ready",
  "prompt.refiner.failed",
  "search.planner.ready",
  "search.interrupted",
  "search.accepted",
  "search.waiting_for_human",
  "search.failed",
  "search.cancelled",
];

const STREAM_END_EVENTS = new Set([
  "search.waiting_for_human",
  "search.accepted",
  "search.failed",
  "search.cancelled",
]);

type OnSearchEvent = (event: SearchEvent) => void;

const PROMPT_EVENT_PRIORITY: Record<string, number> = {
  "prompt.refiner.started": 0,
  "prompt.refiner.ready": 1,
  "prompt.refiner.failed": 1,
};

function promptRound(event: SearchEvent): number {
  const value = event.data.round_index;
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : -1;
}

function numericEventId(event: SearchEvent): number {
  const value = Number(event.id);
  return Number.isSafeInteger(value) && value >= 0 ? value : -1;
}

/** Derive the visible prompt-refiner state without trusting SSE arrival order.
 *
 * EventSource reconnects may replay an older `started` event after `ready`.
 * Round and terminal-state precedence prevent that replay from leaving the UI
 * permanently stuck. Search failures are intentionally not treated as prompt
 * failures: generation or Critic can fail after a valid PromptVersion exists.
 */
export function derivePromptRefinementState(
  events: SearchEvent[],
  currentRound?: number,
): PromptRefinementEventState {
  const promptEvents = events.filter((event) => (
    Object.prototype.hasOwnProperty.call(PROMPT_EVENT_PRIORITY, event.type)
  ));
  const latest = promptEvents.reduce<SearchEvent | undefined>((current, candidate) => {
    if (!current) return candidate;
    const roundDelta = promptRound(candidate) - promptRound(current);
    if (roundDelta !== 0) return roundDelta > 0 ? candidate : current;
    const priorityDelta = PROMPT_EVENT_PRIORITY[candidate.type] - PROMPT_EVENT_PRIORITY[current.type];
    if (priorityDelta !== 0) return priorityDelta > 0 ? candidate : current;
    return numericEventId(candidate) >= numericEventId(current) ? candidate : current;
  }, undefined);
  if (!latest) return { status: "idle" };
  if (typeof currentRound === "number" && promptRound(latest) < currentRound) {
    return { status: "idle" };
  }

  const data = latest.data;
  const modeValue = typeof data.mode === "string"
    ? data.mode
    : typeof data.refinement_mode === "string"
      ? data.refinement_mode
      : "";
  const mode: PromptRefinementMode | undefined = modeValue === "initial" || modeValue === "revision"
    ? modeValue
    : undefined;
  const generationValue = typeof data.generation_mode === "string" ? data.generation_mode : "";
  const generationMode: PromptGenerationMode | undefined = generationValue === "source_rebase"
    || generationValue === "candidate_anchored_rebase"
    ? generationValue
    : undefined;
  const errorValue = data.error;
  const message = typeof data.message === "string"
    ? data.message.slice(0, 600)
    : typeof errorValue === "string"
      ? errorValue.slice(0, 600)
      : typeof errorValue === "object" && errorValue !== null && !Array.isArray(errorValue)
        && typeof (errorValue as Record<string, unknown>).message === "string"
        ? ((errorValue as Record<string, unknown>).message as string).slice(0, 600)
        : undefined;
  const common = {
    roundIndex: promptRound(latest) >= 0 ? promptRound(latest) : undefined,
    mode,
    generationMode,
    requestKey: typeof data.request_key === "string" ? data.request_key.slice(0, 256) : undefined,
  };
  if (latest.type === "prompt.refiner.started") {
    return { ...common, status: "started", message: "正在结合原片、参考图和拍摄信息分析画面。" };
  }
  if (latest.type === "prompt.refiner.failed") {
    return { ...common, status: "failed", message: message ?? "图片分析失败，尚未开始生成。" };
  }
  return { ...common, status: "ready", message: "画面描述已保存，下面可查看实际发送内容。" };
}

function eventFromMessage(message: MessageEvent<string>, forcedType?: string): SearchEvent | null {
  try {
    const parsed: unknown = JSON.parse(message.data);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
    const object = parsed as Record<string, unknown>;
    const payload = typeof object.payload === "object" && object.payload !== null && !Array.isArray(object.payload)
      ? object.payload as Record<string, unknown>
      : {};
    return {
      id: String(object.id ?? message.lastEventId ?? `${Date.now()}`),
      type: String(object.type ?? forcedType ?? "message"),
      created_at: typeof object.created_at === "string" ? object.created_at : undefined,
      data: payload,
    };
  } catch {
    return null;
  }
}

export function useSearchEvents(
  search: SearchRecord | null,
  onSearchEvent?: OnSearchEvent,
  reconnectKey = 0,
): {
  events: SearchEvent[];
  connectionState: EventConnectionState;
} {
  const [events, setEvents] = useState<SearchEvent[]>([]);
  const [connectionState, setConnectionState] = useState<EventConnectionState>("idle");
  const onEventRef = useRef(onSearchEvent);
  const activeSearchIdRef = useRef<string | null>(null);
  const lastEventIdRef = useRef<number>(0);
  const seenEventIdsRef = useRef(new Set<string>());
  const seenEventOrderRef = useRef<string[]>([]);

  useEffect(() => {
    onEventRef.current = onSearchEvent;
  }, [onSearchEvent]);

  useEffect(() => {
    if (!search) {
      activeSearchIdRef.current = null;
      lastEventIdRef.current = 0;
      seenEventIdsRef.current.clear();
      seenEventOrderRef.current = [];
      setEvents([]);
      setConnectionState("idle");
      return;
    }
    if (activeSearchIdRef.current !== search.search_id) {
      activeSearchIdRef.current = search.search_id;
      lastEventIdRef.current = 0;
      seenEventIdsRef.current.clear();
      seenEventOrderRef.current = [];
      setEvents([]);
    }
    if (typeof EventSource === "undefined") {
      setConnectionState("unavailable");
      return;
    }

    setConnectionState("connecting");
    const baseUrl = searchEventsUrl(search);
    const after = lastEventIdRef.current > 0
      ? `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}after=${lastEventIdRef.current}`
      : baseUrl;
    const source = new EventSource(after);
    const receive = (message: MessageEvent<string>, forcedType?: string) => {
      const event = eventFromMessage(message, forcedType);
      if (!event || seenEventIdsRef.current.has(event.id)) return;
      seenEventIdsRef.current.add(event.id);
      seenEventOrderRef.current.push(event.id);
      if (seenEventOrderRef.current.length > 256) {
        const expiredId = seenEventOrderRef.current.shift();
        if (expiredId) seenEventIdsRef.current.delete(expiredId);
      }
      const numericId = Number(event.id);
      if (Number.isSafeInteger(numericId) && numericId > lastEventIdRef.current) {
        lastEventIdRef.current = numericId;
      }
      setEvents((current) => [...current.slice(-79), event]);
      onEventRef.current?.(event);
      if (STREAM_END_EVENTS.has(event.type)) {
        source.close();
        setConnectionState("settled");
      }
    };

    const namedListeners = EVENT_TYPES.map((type) => {
      const listener: EventListener = (event) => receive(event as MessageEvent<string>, type);
      source.addEventListener(type, listener);
      return [type, listener] as const;
    });

    source.onmessage = (event) => receive(event);
    source.onopen = () => setConnectionState("open");
    source.onerror = () => setConnectionState("reconnecting");

    return () => {
      namedListeners.forEach(([type, listener]) => source.removeEventListener(type, listener));
      source.close();
    };
  }, [search?.search_id, search?.events_url, search?.status, reconnectKey]);

  return { events, connectionState };
}
