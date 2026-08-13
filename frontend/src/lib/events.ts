import { useEffect, useRef, useState } from "react";
import type { SearchEvent, SearchRecord } from "../types";
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
  "round.generation.started",
  "round.candidate.ready",
  "round.critic.started",
  "round.evaluation.ready",
  "round.winner.updated",
  "search.global_winner.updated",
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
): {
  events: SearchEvent[];
  connectionState: EventConnectionState;
} {
  const [events, setEvents] = useState<SearchEvent[]>([]);
  const [connectionState, setConnectionState] = useState<EventConnectionState>("idle");
  const onEventRef = useRef(onSearchEvent);

  useEffect(() => {
    onEventRef.current = onSearchEvent;
  }, [onSearchEvent]);

  useEffect(() => {
    setEvents([]);
    if (!search) {
      setConnectionState("idle");
      return;
    }
    if (typeof EventSource === "undefined") {
      setConnectionState("unavailable");
      return;
    }

    setConnectionState("connecting");
    const source = new EventSource(searchEventsUrl(search));
    const seenIds = new Set<string>();

    const receive = (message: MessageEvent<string>, forcedType?: string) => {
      const event = eventFromMessage(message, forcedType);
      if (!event || seenIds.has(event.id)) return;
      seenIds.add(event.id);
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
  }, [search?.search_id, search?.events_url]);

  return { events, connectionState };
}
