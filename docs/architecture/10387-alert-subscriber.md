Task 10387 - Architecture Spec: KAI AlertSubscriber
Author: Architect
Date: 2026-05-07
Status: final architecture artifact

Scope
=====

Design a daemon-owned KAI AlertSubscriber that consumes configured NATS alert
subjects and injects bounded alert prompts into target daemon sessions. The
service must be shaped parallel to HeartbeatService, reuse the heartbeat prompt
injection path through a new EventInjector abstraction, and preserve the
daemon's existing at-most-once, no-backlog behavior for event-driven agent
wakeups.

Files inspected:

- docs/architecture/10374-heartbeat-phase-2.md
- daemon/heartbeat.py
- daemon/server.py around HeartbeatService startup, _handle_heartbeat_tick(),
  run_input(), metrics_snapshot(), and health_snapshot()
- daemon/core.py around start_auto_mode(), heartbeat subscription state, save/load,
  and stream_agent_events()
- agent/auto_evaluator.py
- agent-config.json
- agent/signal_consumer.py, agent/signal_handlers.py, and nats_bus/bus.py as
  adjacent NATS/config precedents

Existing Baseline
=================

Heartbeat Phase 2 is already implemented in the repo, not just documented:

- daemon/heartbeat.py defines HeartbeatTick, HeartbeatConfig,
  HeartbeatPromptTemplate, load_heartbeat_config(), and HeartbeatService.
- DaemonServer.__init__ loads HeartbeatConfig from get_agent_config(agent_name)
  and loads the prompt template once.
- DaemonServer.startup() connects NATS, subscribes SignalConsumer, starts
  Scheduler, then starts HeartbeatService.
- DaemonServer._handle_heartbeat_tick() first publishes passive daemon/session
  heartbeat events, then wakes eligible auto-mode sessions in background tasks.
- DaemonServer._heartbeat_injection_decision() gates heartbeat injection on
  subscription, auto mode, max_injected_turns_per_hour, input_lock,
  current_input_task, runtime attachment, auto-continuation state, active tool
  calls, _heartbeat_turn_active, and per-session rate limit.
- DaemonServer._run_heartbeat_turn() renders the heartbeat template, appends a
  HumanMessage, publishes auto.heartbeat_injected, then calls run_input() with
  source="heartbeat", single_auto_iteration=True, and pre_injected_input=True.
- Session.start_auto_mode() supports heartbeat_subscribed, persists
  heartbeat_subscribed / heartbeat_subscription_configured, and keeps a deque of
  successful heartbeat injection timestamps.
- AgentRunner.run(pre_injected_input=True) avoids appending the same visible
  user input twice.
- /api/metrics and /api/health include heartbeat status and subscribers_count.

That baseline is the correct pattern for AlertSubscriber. The new work should
not create a second ad hoc injection path. It should extract and reuse the
general "event payload -> rendered prompt -> guarded one-turn run" mechanics
that heartbeat currently owns inside DaemonServer.

Design Goals
============

1. Provide a daemon-owned AlertSubscriber service shaped parallel to
   HeartbeatService: config model, payload model, background lifecycle,
   subscribe/start/shutdown, counters, last event, and a callback into
   DaemonServer.
2. Subscribe to NATS once per enabled
   (subject_pattern, prompt_template, target_session) tuple.
3. Normalize incoming NATS payloads into a strict JSON-ready alert contract.
4. Treat malformed payloads conservatively: never inject malformed data into
   chat history, never crash the NATS callback, and surface malformed counters.
5. Refactor heartbeat plumbing by extracting a shared EventInjector with exact
   fields/methods listed below.
6. Add daemon.alert_subscriber config with a disabled-by-default polymarket
   subscription, env overrides, global kill switch, and optional alerts.yaml
   overlay.
7. Implement suppression, rate limiting, and deduplication per subscription and
   target session.
8. Add telemetry and expose /api/health.alert_subscribers.
9. Define failure behavior for NATS reconnect, malformed messages, handler
   exceptions, target session absence, busy sessions, and at-most-once delivery.
10. Deliver as a phased ticket plan, phases 0-6, with decision gates and
    realistic ETA.

Non-Goals
=========

- Do not replace SignalConsumer. SignalConsumer remains the bounded historical
  signal buffer used by tools/UI for signals. AlertSubscriber is a wakeup path
  for configured alert prompts.
- Do not migrate the existing TUI SignalHandlerRunner in this task. It is useful
  prior art but is synchronous/TUI-oriented and supports broader action verbs.
- Do not persist unprocessed alerts or add durable retry queues in v1.
- Do not guarantee exactly-once alert handling. v1 is at-most-once from the
  daemon's perspective.
- Do not auto-enable trading or execution prompts. Default subscriptions must be
  disabled, and any trading-sensitive prompt must remain opt-in.

High-Level Architecture
=======================

Add daemon/alert_subscriber.py with an AlertSubscriberService that owns NATS
subscriptions and emits normalized AlertEvent objects to DaemonServer. Add
daemon/event_injector.py with EventInjector, a reusable injection coordinator
used by both heartbeat and alert subscribers.

Flow:

1. DaemonServer.startup() connects NATS as it does today.
2. DaemonServer starts AlertSubscriberService after bus connection and before or
   near HeartbeatService startup.
3. AlertSubscriberService reads daemon.alert_subscriber subscriptions from
   agent-config.json plus optional alerts.yaml overlay and env overrides.
4. For every enabled subscription tuple, it calls bus.subscribe(subject_pattern,
   handler).
5. On each NATS message, the service validates/normalizes JSON payload into
   AlertEvent and calls DaemonServer._handle_alert_event(alert_event).
6. DaemonServer publishes daemon telemetry and asks EventInjector to inject the
   alert into the target session.
7. EventInjector applies shared suppression/rate-limit/dedup policy, renders the
   configured prompt template, appends exactly one HumanMessage, emits telemetry,
   and invokes run_input(..., source="alert_subscriber",
   single_auto_iteration=True, pre_injected_input=True).
8. If any gate fails, the event is dropped. No queue is created.

Recommended module split:

- daemon/alert_subscriber.py
  - AlertSubscriberConfig
  - AlertSubscriptionConfig
  - AlertEvent
  - AlertSubscriberService
  - load_alert_subscriber_config()
  - load_alerts_yaml()
- daemon/event_injector.py
  - EventInjectionTemplate
  - EventInjectionRequest
  - EventInjectionPolicy
  - EventInjectionDecision
  - EventInjector
  - SafeFormatDict or shared formatter
- daemon/heartbeat.py
  - Keep HeartbeatService and HeartbeatTick.
  - Either keep HeartbeatPromptTemplate as a compatibility alias around
    EventInjectionTemplate or move generic prompt loading into
    daemon/event_injector.py and update heartbeat imports.
- daemon/server.py
  - Own one EventInjector.
  - Wire HeartbeatService and AlertSubscriberService through EventInjector.
  - Extend metrics_snapshot() and health_snapshot().

Service Shape Parallel to HeartbeatService
==========================================

AlertSubscriberService should mirror HeartbeatService's operational shape:

Data classes:

    @dataclass(frozen=True)
    class AlertSubscriberConfig:
        enabled: bool = False
        kill_switch: bool = False
        alerts_yaml_path: str | None = "alerts.yaml"
        subscriptions: tuple[AlertSubscriptionConfig, ...] = ()
        default_max_injected_turns_per_hour: int = 10
        default_dedup_ttl_seconds: float = 900.0
        default_busy_drop: bool = True

    @dataclass(frozen=True)
    class AlertSubscriptionConfig:
        name: str
        enabled: bool
        subject_pattern: str
        prompt_template_path: str
        target_session: str
        target_agent: str | None = None
        require_auto_mode: bool = False
        create_session_if_missing: bool = True
        max_injected_turns_per_hour: int | None = None
        min_interval_seconds: float = 0.0
        dedup_ttl_seconds: float | None = None
        dedup_fields: tuple[str, ...] = ("source", "alert_type", "market", "symbol", "event_id")
        malformed_policy: str = "drop"
        single_auto_iteration: bool = True
        readonly: bool | None = None

    @dataclass(frozen=True)
    class AlertEvent:
        type: str
        subscription_name: str
        subject: str
        received_at: str
        monotonic_seconds: float
        seq: int
        payload: dict[str, Any]
        dedup_key: str
        source: str
        alert_type: str
        severity: str
        target_session: str
        prompt_template_path: str

Service methods:

    class AlertSubscriberService:
        def __init__(
            self,
            *,
            config: AlertSubscriberConfig,
            bus: Any | None,
            alert_callback: Callable[[AlertEvent], Awaitable[None]],
            clock: Callable[[], datetime] = utc_now,
        ) -> None: ...

        @property
        def running(self) -> bool: ...

        async def start(self) -> None: ...
        async def shutdown(self) -> None: ...
        async def handle_message(
            self,
            subscription: AlertSubscriptionConfig,
            subject: str,
            payload: dict[str, Any],
        ) -> AlertEvent | None: ...

Operational fields:

- _subscriptions: list[Any] returned by NatsBus.subscribe()
- _seq: int
- last_event: AlertEvent | None
- received_count: int
- injected_request_count: int
- malformed_count: int
- dropped_count: int
- failure_count: int
- subscribed_count: int
- subscription_stats: dict[str, AlertSubscriptionStats]

NATS Subscription Contract
==========================

One NATS subscription is created per enabled tuple:

    (subject_pattern, prompt_template, target_session)

The tuple is intentionally concrete. If two configured rows use the same
subject_pattern but different prompt_template or target_session, both rows
receive the event and are independently gated/deduped/rate-limited.

Example:

    {
      "name": "polymarket-large-order-to-kai",
      "enabled": false,
      "subject_pattern": "polymarket.alpha.alarm.>",
      "prompt_template_path": "prompts/alerts/polymarket.md.tmpl",
      "target_session": "kai",
      "target_agent": "kai"
    }

NatsBus.subscribe() currently JSON-decodes incoming data; malformed JSON is
converted to {"raw": "..."} before calling the handler. AlertSubscriber must
treat that as malformed unless a subscription explicitly accepts raw text in a
future phase. v1 requires object payloads with a minimal alert envelope.

Payload JSON Contract
=====================

Incoming NATS payloads should be JSON objects. The normalized v1 contract is:

    {
      "source": "polymarket",
      "alert_type": "market_move",
      "severity": "info|warning|critical",
      "event_id": "optional-upstream-id",
      "market": "optional market slug or id",
      "symbol": "optional symbol",
      "title": "short human-readable title",
      "summary": "short body suitable for prompt rendering",
      "url": "optional source URL",
      "timestamp": "2026-05-07T19:05:00Z",
      "data": {
        "provider_specific": "fields"
      }
    }

Required fields after normalization:

- source: non-empty string; default to first subject token after alerts if absent
- alert_type: non-empty string; default to "unknown" if absent
- severity: one of info, warning, critical; invalid values become "info"
- title or summary: at least one non-empty string
- timestamp: ISO-ish string; if absent, use received_at
- data: object; if absent, {}

Fields added by the daemon before rendering:

- subscription_name
- subject
- received_at
- seq
- target_session
- dedup_key

Malformed Handling
==================

Malformed payloads are dropped by default.

Malformed includes:

- payload is not a dict
- payload contains only "raw" from NatsBus JSON decode fallback
- title and summary are both absent/empty
- data exists but is not an object
- configured required_fields are missing, if a subscription adds that option

Handling rules:

- Do not call EventInjector.
- Increment service.malformed_count and per-subscription malformed_count.
- Publish a daemon-scoped event on event_bus channel "alert_subscriber" with:

    {
      "type": "alert_subscriber.malformed",
      "subscription_name": "...",
      "subject": "...",
      "reason": "non_json|missing_text|invalid_data|missing_required_field",
      "received_at": "...",
      "payload_keys": ["safe", "top", "level", "keys"]
    }

- Do not include the raw payload body in telemetry by default. It may contain
  user data, URLs, or provider secrets.
- Log a warning with subscription name, subject, reason, and safe keys.
- Never raise out of the NATS callback for malformed input.

Prompt Template Contract
========================

Alert prompt templates should use the same simple format_map style as heartbeat
templates. Do not add Jinja in v1.

Recommended default template path:

    prompts/alerts/polymarket.md.tmpl

Template variables:

- {subscription_name}
- {subject}
- {seq}
- {received_at}
- {source}
- {alert_type}
- {severity}
- {event_id}
- {market}
- {symbol}
- {title}
- {summary}
- {url}
- {timestamp}
- {dedup_key}
- {target_session}
- {payload_json}
- {data_json}

Unknown variables should remain literal, matching HeartbeatPromptTemplate's
SafeFormatDict behavior, so template mistakes are visible and testable.

Suggested polymarket template:

    Alert from {source}: {title}

    Severity: {severity}
    Type: {alert_type}
    Market: {market}
    Symbol: {symbol}
    Time: {timestamp}
    URL: {url}

    Summary:
    {summary}

    Review this alert in context. Be concise, identify whether action is
    warranted, and do not place trades or external orders unless an explicit
    user policy already authorizes that action.

EventInjector Refactor Proposal
===============================

Current heartbeat injection fields/methods to move out of DaemonServer and into
a shared EventInjector:

Move these DaemonServer-owned responsibilities:

- heartbeat_prompt_template field
- heartbeat_subscribers_count() general counting logic should become
  EventInjector.subscribers_count(source: str | None = None) or be split into
  heartbeat-specific and alert-specific health helpers.
- _heartbeat_injection_decision()
- _publish_heartbeat_drop()
- _run_heartbeat_turn()
- The common parts of _handle_heartbeat_tick() after passive tick fanout:
  iterate sessions, decide, render, append HumanMessage, publish injected/drop
  telemetry, call run_input(), reset active flag.

Move or generalize these Session fields:

- heartbeat_injection_timestamps should remain for compatibility but new generic
  storage should be added:

    self.event_injection_timestamps: dict[str, deque[float]] = {}
    self.event_injection_dedup: dict[str, float] = {}
    self._event_injection_active: set[str] = set()

- Keep heartbeat_subscribed and _heartbeat_turn_active during transition, but
  EventInjector should support source-specific active keys such as "heartbeat"
  and "alert_subscriber:<subscription_name>".

New generic data types:

    @dataclass(frozen=True)
    class EventInjectionTemplate:
        name: str
        path: Path
        content: str

        @classmethod
        def load(cls, template_path: str | Path) -> "EventInjectionTemplate": ...
        def render(self, variables: Mapping[str, Any]) -> str: ...

    @dataclass(frozen=True)
    class EventInjectionPolicy:
        source: str
        subscription_name: str | None = None
        target_session: str = "kai"
        require_auto_mode: bool = False
        require_subscription_flag: bool = False
        max_injected_turns_per_hour: int = 4
        min_interval_seconds: float = 0.0
        dedup_ttl_seconds: float = 0.0
        dedup_key: str | None = None
        single_auto_iteration: bool = True
        source_event_topic_injected: str = "auto.event_injected"
        source_event_topic_dropped: str = "auto.event_dropped"

    @dataclass(frozen=True)
    class EventInjectionRequest:
        source: str
        event_name: str
        seq: int
        target_session: str
        template: EventInjectionTemplate
        template_variables: dict[str, Any]
        policy: EventInjectionPolicy
        raw_event_payload: dict[str, Any]
        monotonic_seconds: float
        job_id: str

    @dataclass(frozen=True)
    class EventInjectionDecision:
        ok: bool
        reason: str

EventInjector fields:

    class EventInjector:
        def __init__(
            self,
            *,
            sessions: MutableMapping[str, ManagedSession],
            run_input: Callable[..., Awaitable[InputRunResult]],
            event_bus: DaemonEventBus,
            logger: Any,
        ) -> None:
            self.sessions = sessions
            self.run_input = run_input
            self.event_bus = event_bus
            self.log = logger
            self.injected_count = 0
            self.dropped_count = 0
            self.failure_count = 0
            self.last_injection: dict[str, Any] | None = None
            self.source_counts: Counter[str] = Counter()
            self.source_drops: Counter[str] = Counter()

EventInjector methods:

    def subscribers_count(self, *, source: str | None = None) -> int: ...

    async def inject_or_drop(self, request: EventInjectionRequest) -> EventInjectionDecision:
        """Find target session, apply decision, schedule one background turn."""

    def injection_decision(
        self,
        managed: ManagedSession,
        request: EventInjectionRequest,
    ) -> EventInjectionDecision: ...

    def publish_drop(
        self,
        managed: ManagedSession | None,
        request: EventInjectionRequest,
        reason: str,
    ) -> None: ...

    async def run_injection_turn(
        self,
        managed: ManagedSession,
        request: EventInjectionRequest,
    ) -> None: ...

    def _active_key(self, request: EventInjectionRequest) -> str: ...
    def _rate_key(self, request: EventInjectionRequest) -> str: ...
    def _dedup_key(self, request: EventInjectionRequest) -> str | None: ...

Heartbeat after refactor:

- HeartbeatService stays unchanged.
- DaemonServer._handle_heartbeat_tick() keeps passive heartbeat event fanout.
- For each eligible session or through one request per session, build an
  EventInjectionRequest with:
  - source="heartbeat"
  - event_name="heartbeat.tick"
  - target_session=managed.session.name
  - template=heartbeat template
  - policy.require_auto_mode=True
  - policy.require_subscription_flag=True
  - policy.max_injected_turns_per_hour=heartbeat_config.max_injected_turns_per_hour
  - telemetry topics auto.heartbeat_injected / auto.heartbeat_dropped
  - job_id=f"heartbeat:{tick.seq}"
- This preserves all current tests while making AlertSubscriber reuse the same
  append/pre_injected/run_input mechanics.

AlertSubscriber EventInjector use:

- AlertSubscriberService produces one AlertEvent per subscription match.
- DaemonServer._handle_alert_event() loads or looks up the prompt template and
  calls EventInjector.inject_or_drop().
- source="alert_subscriber"
- event_name="alert.received"
- job_id=f"alert:{subscription_name}:{seq}"
- telemetry topics auto.alert_injected / auto.alert_dropped

Decision Rules for Alert Injection
==================================

EventInjector.injection_decision() should apply these rules in order:

1. target_session_missing
   - If create_session_if_missing is false and no live/indexed session exists,
     drop.
   - If true, DaemonServer._handle_alert_event() may call get_or_create_session()
     before EventInjector.

2. runtime_not_attached
   - Drop if session.agent_runner is None.

3. auto_mode_required_but_disabled
   - Drop if policy.require_auto_mode is true and session.auto_mode is false.
   - For default alerts, require_auto_mode should be false so KAI can be woken
     by an alert even when idle.

4. not_subscribed
   - Only applies when require_subscription_flag is true, preserving heartbeat
     behavior.

5. kill_switch
   - Global alert kill switch blocks all alert injections but can still allow
     passive malformed/received counters.

6. busy
   - Drop when managed.input_lock.locked() or current_input_task is active.
   - Do not queue.

7. auto_continuing
   - Drop when runner._is_auto_continuation is true.

8. mid_tool_call
   - Drop when runner.tool_call_active is true or runner._active_recorder is set.

9. source_active
   - Drop when the source/subscription active key is already in
     session._event_injection_active.

10. deduped
   - Drop when dedup_key exists and has not expired.

11. min_interval
   - Drop when the last successful injection for this source/subscription is
     newer than min_interval_seconds.

12. rate_limited
   - Drop when successful injections in the last 3600 seconds are >=
     max_injected_turns_per_hour.

13. ok
   - Render and run exactly one turn.

Suppression, Rate Limit, Dedup
==============================

Suppression:

- Busy sessions drop alerts immediately.
- No alert should be appended to input_queue.
- Drop events should be emitted for operational visibility.
- Do not publish drop events for disabled subscriptions unless debug telemetry is
  enabled; disabled config should be quiet.

Rate limit:

- Use a per-session, per-subscription sliding one-hour window.
- Defaults:
  - daemon.alert_subscriber.default_max_injected_turns_per_hour: 10
  - per subscription max_injected_turns_per_hour overrides default
  - 0 disables injection for that subscription without disabling subscription
    counters
- Count only successful prompt injections, not malformed or busy drops.

Min interval:

- Optional per subscription min_interval_seconds.
- Enforced in addition to hourly rate limit.
- Useful for noisy alert streams where dedup fields vary.

Dedup:

- Build a stable dedup key from configured dedup_fields. Default:
  source, alert_type, market, symbol, event_id.
- If event_id is missing, include title + summary hash in the dedup key.
- Store dedup keys in-memory with expiry.
- Default TTL: 900 seconds.
- Dedup is per subscription and target_session, not global, so two sessions can
  intentionally receive the same alert.

Config Contract
===============

Add daemon.alert_subscriber to agent-config.json. The framework default is enabled=true, while the first concrete polymarket subscription ships enabled=false so operators intentionally opt in per source:

    {
      "daemon": {
        "alert_subscriber": {
          "enabled": true,
          "kill_switch": false,
          "alerts_yaml_path": "alerts.yaml",
          "default_max_injected_turns_per_hour": 10,
          "default_dedup_ttl_seconds": 900,
          "subscriptions": [
            {
              "name": "polymarket-default",
              "enabled": false,
              "subject_pattern": "polymarket.alpha.alarm.>",
              "prompt_template_path": "prompts/alerts/polymarket.md.tmpl",
              "target_session": "kai",
              "target_agent": "kai",
              "require_auto_mode": false,
              "create_session_if_missing": true,
              "max_injected_turns_per_hour": 10,
              "min_interval_seconds": 120,
              "dedup_ttl_seconds": 900,
              "dedup_fields": ["source", "alert_type", "market", "symbol", "event_id"]
            }
          ]
        }
      }
    }

Default polymarket subscription:

- Present in config or documented as built-in default.
- enabled=false by default.
- subject_pattern="polymarket.alpha.alarm.>"
- prompt_template_path="prompts/alerts/polymarket.md.tmpl"
- target_session="kai"
- target_agent="kai"
- max_injected_turns_per_hour=10
- min_interval_seconds=120
- dedup_ttl_seconds=900

Environment overrides:

- KAI_ALERT_SUBSCRIBER_ENABLED=0|1
- KAI_ALERT_SUBSCRIBER_KILL_SWITCH=0|1
- KAI_ALERT_SUBSCRIBER_ALERTS_YAML_PATH=alerts.yaml
- KAI_ALERT_SUBSCRIBER_DEFAULT_MAX_INJECTED_TURNS_PER_HOUR=10
- KAI_ALERT_SUBSCRIBER_DEFAULT_DEDUP_TTL_SECONDS=900
- KAI_ALERT_SUBSCRIBER_ENABLE_POLYMARKET=0|1
- KAI_ALERT_SUBSCRIBER_POLYMARKET_SUBJECT=polymarket.alpha.alarm.>
- KAI_ALERT_SUBSCRIBER_POLYMARKET_TARGET_SESSION=kai
- KAI_ALERT_SUBSCRIBER_POLYMARKET_TEMPLATE=prompts/alerts/polymarket.md.tmpl

Kill switch semantics:

- If KAI_ALERT_SUBSCRIBER_KILL_SWITCH is true, no alert prompt injection occurs.
- The service may still subscribe and count received/malformed events if enabled,
  but injected_count must remain 0 and drops should use reason="kill_switch".
- If daemon.alert_subscriber.enabled is false, the service should not subscribe. Framework default is true; individual source subscriptions, including polymarket, default false.

alerts.yaml option:

- If alerts_yaml_path exists, load it and merge subscriptions after
  agent-config.json.
- YAML entries with the same name override JSON entries.
- Missing alerts.yaml is not an error.
- Invalid YAML should fail daemon startup only when
  daemon.alert_subscriber.enabled=true; otherwise log a warning and continue.
- This allows operational changes without editing the large agent-config.json.

Example alerts.yaml:

    subscriptions:
      - name: polymarket-default
        enabled: true
        subject_pattern: polymarket.alpha.alarm.>
        prompt_template_path: prompts/alerts/polymarket.md.tmpl
        target_session: kai
        target_agent: kai
        max_injected_turns_per_hour: 10
        min_interval_seconds: 120
        dedup_ttl_seconds: 900

Telemetry
=========

Telemetry topics mirror heartbeat naming and should be emitted through daemon/session event paths as applicable: auto.alert_received, auto.alert_dropped, auto.alert_injected, auto.alert_error.

Received event topic: auto.alert_received

    {
      "type": "auto.alert_received",
      "subscription_name": "polymarket-default",
      "subject": "polymarket.alpha.alarm.market_move",
      "seq": 12,
      "dedup_key": "...",
      "target_session": "kai",
      "source": "polymarket",
      "alert_type": "market_move",
      "severity": "warning",
      "received_at": "2026-05-07T19:00:00Z"
    }

Injected session event topic: auto.alert_injected

    {
      "subscription_name": "polymarket-default",
      "subject": "polymarket.alpha.alarm.market_move",
      "seq": 12,
      "template_name": "polymarket.md.tmpl",
      "chars_injected": 614,
      "dedup_key": "...",
      "source": "polymarket",
      "alert_type": "market_move"
    }

Dropped session event topic: auto.alert_dropped (reason includes malformed_payload when validation fails before injection)

    {
      "subscription_name": "polymarket-default",
      "subject": "polymarket.alpha.alarm.market_move",
      "seq": 12,
      "reason": "busy|deduped|rate_limited|min_interval|kill_switch|runtime_not_attached|target_session_missing|template_render_failed",
      "dedup_key": "..."
    }

Error daemon/session event topic: auto.alert_error

    {
      "type": "auto.alert_error",
      "subscription_name": "polymarket-default",
      "subject": "polymarket.alpha.alarm.market_move",
      "reason": "missing_text",
      "received_at": "2026-05-07T19:00:00Z",
      "payload_keys": ["source", "alert_type"]
    }

Counters:

- received_count
- injected_count
- dropped_count
- malformed_count
- deduped_count
- rate_limited_count
- failure_count
- reconnect_count if the underlying bus exposes callbacks later
- per-subscription counters for all of the above

/api/health.alert_subscribers Shape
===================================

Extend health_snapshot() with a new top-level key, not nested under heartbeat:

    {
      "alert_subscribers": {
        "enabled": true,
        "kill_switch": false,
        "running": true,
        "bus_connected": true,
        "configured_count": 1,
        "subscribed_count": 1,
        "received_count": 42,
        "injected_count": 8,
        "dropped_count": 28,
        "malformed_count": 2,
        "failure_count": 0,
        "last_event": {
          "subscription_name": "polymarket-default",
          "subject": "polymarket.alpha.alarm.market_move",
          "seq": 42,
          "received_at": "2026-05-07T19:00:00Z",
          "target_session": "kai",
          "source": "polymarket",
          "alert_type": "market_move",
          "severity": "warning"
        },
        "subscriptions": {
          "polymarket-default": {
            "enabled": true,
            "subject_pattern": "polymarket.alpha.alarm.>",
            "target_session": "kai",
            "prompt_template_name": "polymarket.md.tmpl",
            "subscribed": true,
            "received_count": 42,
            "injected_count": 8,
            "dropped_count": 28,
            "malformed_count": 2,
            "last_received_at": "2026-05-07T19:00:00Z",
            "last_drop_reason": "busy"
          }
        }
      }
    }

Extend metrics_snapshot() with the same object or a more detailed version. Keep
health compact and JSON-safe.

Failure Modes
=============

NATS unavailable at daemon startup:

- Existing DaemonServer startup catches bus connect failures and sets bus=None.
- AlertSubscriberService should report enabled=true, running=false,
  bus_connected=false, subscribed_count=0.
- Do not crash daemon solely because alert subscriber cannot subscribe.

NATS reconnect:

- nats-py handles reconnect inside NatsBus.connect() with reconnect_time_wait=2
  and max_reconnect_attempts=10.
- Existing subscriptions should normally survive reconnect in nats-py. v1 should
  rely on that behavior and expose bus_connected.
- If NatsBus later exposes reconnect callbacks, AlertSubscriberService should
  increment reconnect_count and verify subscription count after reconnect.

Subscription failure:

- If one subscription fails, increment failure_count and continue attempting the
  remaining subscriptions.
- running=true can mean service started; subscribed_count may be less than
  configured_count. Health must show the mismatch.

Malformed message:

- Drop, count, telemetry, no exception out of callback.

Template missing:

- If a subscription is enabled and its template is missing/unreadable, fail
  daemon startup when alert_subscriber.enabled=true.
- This mirrors heartbeat's prompt drift stance.
- Disabled subscriptions do not need template load validation until enabled.

Template render failure:

- Drop that event.
- Increment dropped_count/failure_count as appropriate.
- Publish auto.alert_dropped with reason=template_render_failed when a target
  session exists; otherwise daemon event only.

Target session missing:

- If create_session_if_missing=true, DaemonServer creates/hydrates the session
  and attaches runtime.
- If creation fails, drop with target_session_missing or session_create_failed.
- If create_session_if_missing=false, drop.

Busy session:

- Drop immediately. Do not queue.
- Reason busy.

Rate limited or deduped:

- Drop immediately. Do not queue.
- Reasons rate_limited, min_interval, or deduped.

Alert-triggered agent run crashes:

- run_input() already catches exceptions and publishes agent.error.
- EventInjector must reset the active key in finally.
- Background task exceptions must be consumed with the existing
  _consume_background_task_exception pattern.

At-most-once semantics:

- A NATS callback attempts at most one EventInjector request per subscription
  match.
- If the daemon is down, disconnected, busy, rate-limited, or crashes during the
  callback, the alert is lost from AlertSubscriber's perspective.
- No durable replay, ack protocol, or retry queue is introduced in v1.
- This matches the heartbeat no-backlog model and avoids stale alerts causing
  surprise future agent turns.

Auto Evaluator Interaction
==========================

agent/auto_evaluator.py validates strict evaluator JSON and defaults malformed
or low-confidence evaluator output to STOP. AlertSubscriber should follow that
same conservative boundary principle:

- Validate external alert JSON before prompt rendering.
- Treat invalid/malformed input as a drop, not a best-effort freeform prompt.
- Use single_auto_iteration=True for alert turns by default, so one external
  alert cannot trigger a hidden AUTO_STATE continuation loop.
- If the alert turn itself returns AUTO_STATE: done or pause, existing
  stream_agent_events() semantics may stop auto mode. If that is undesirable for
  alert turns, add a later policy flag, but v1 should reuse existing heartbeat
  behavior.

Security and Safety
===================

- Never include raw malformed payloads in telemetry.
- Prompt templates should explicitly forbid trade/order placement unless a
  separate explicit user policy authorizes it.
- Default polymarket subscription must be disabled.
- Global kill switch must be env-controllable.
- Keep all behavior local to configured target_session; do not broadcast alert
  prompts to every session.
- No durable storage of alert payloads in v1 beyond normal session chat history
  when an injection succeeds.

Testing Plan
============

Unit tests:

- load_alert_subscriber_config({}) returns enabled false and a disabled
  polymarket subscription.
- Env KAI_ALERT_SUBSCRIBER_ENABLED and KAI_ALERT_SUBSCRIBER_KILL_SWITCH override
  config.
- Env KAI_ALERT_SUBSCRIBER_ENABLE_POLYMARKET enables only the default
  polymarket row.
- alerts.yaml overlay overrides a JSON subscription by name.
- Missing alerts.yaml is ignored.
- Invalid alerts.yaml fails only when alert_subscriber.enabled=true.
- AlertEvent normalization accepts the v1 contract and fills defaults.
- Malformed cases increment malformed_count and never call callback.
- Dedup key is stable, includes fallback title/summary hash when event_id is
  absent, and expires after TTL.
- EventInjector decision drops busy, mid_tool_call, auto_continuing, deduped,
  min_interval, rate_limited, missing runtime, and kill_switch cases.
- EventInjectionTemplate render preserves unknown placeholders literally.

Integration tests:

- Fake NatsBus captures subscribe calls; AlertSubscriberService subscribes once
  per enabled tuple.
- Same subject with two different target sessions creates two subscriptions and
  two independent callbacks.
- Valid alert to a live target session appends exactly one HumanMessage and
  AgentRunner receives the same prompt with pre_injected_input=True.
- Busy target session drops without mutating chat_history or input_queue.
- Rate limit drops second alert when max_injected_turns_per_hour=1.
- Dedup drops duplicate event_id within TTL.
- /api/health includes alert_subscribers shape with subscribed_count,
  malformed_count, injected_count, and per-subscription stats.
- Heartbeat tests still pass after EventInjector extraction.

E2E/smoke:

- Start daemon with a fake/local NATS server or fake bus.
- Enable one alert subscription with short rate limits.
- Publish one valid alert.
- Verify /api/health.alert_subscribers.received_count increments.
- Verify session events include auto.alert_injected.
- Publish malformed JSON/raw payload and verify malformed_count increments.
- Publish duplicate and verify deduped drop.

Phased Ticket Plan
==================

Phase 0 - Discovery and guardrails

Deliverables:

- Confirm current heartbeat tests are green before refactor.
- Add architecture notes to implementation ticket with the exact public contracts
  above.
- Identify whether PyYAML is already a dependency. If not, decide whether
  alerts.yaml waits until Phase 2 or uses a minimal optional import.

Decision gate:

- Proceed only if heartbeat behavior is covered by tests and the team accepts
  at-most-once/no-backlog semantics for alerts.

ETA: 0.5 day.

Phase 1 - EventInjector extraction

Deliverables:

- Add daemon/event_injector.py.
- Move generic prompt template loading/rendering into EventInjectionTemplate.
- Move heartbeat decision/render/append/run/drop logic from DaemonServer into
  EventInjector while preserving heartbeat topics and payloads.
- Keep HeartbeatService and HeartbeatTick unchanged.
- Keep existing heartbeat config unchanged.
- Existing heartbeat tests pass with minimal assertion updates if import paths
  change.

Decision gate:

- No AlertSubscriber code starts until heartbeat behavior is unchanged after
  extraction.

ETA: 1.5-2 days.

Phase 2 - AlertSubscriber config and templates

Deliverables:

- Add AlertSubscriberConfig and AlertSubscriptionConfig.
- Implement load_alert_subscriber_config().
- Add disabled default polymarket subscription.
- Add env overrides and kill switch.
- Add optional alerts.yaml overlay.
- Add prompts/alerts/polymarket.md.tmpl.
- Add config unit tests.

Decision gate:

- Config must be disabled by default, env-enableable in tests, and fail clearly
  for enabled subscriptions with missing templates.

ETA: 1 day.

Phase 3 - NATS subscription service

Deliverables:

- Add AlertSubscriberService with HeartbeatService-like start/shutdown/running
  lifecycle.
- Subscribe once per enabled tuple.
- Normalize payloads into AlertEvent.
- Implement malformed handling and counters.
- Add fake bus tests.

Decision gate:

- Valid and malformed NATS callbacks must not crash the service. Health counters
  must be deterministic under tests.

ETA: 1-1.5 days.

Phase 4 - DaemonServer integration and health

Deliverables:

- Instantiate AlertSubscriberService in DaemonServer after bus connect.
- Add _handle_alert_event().
- Use EventInjector for alert injection.
- Add alert_subscribers to metrics_snapshot() and health_snapshot().
- Ensure shutdown drains/cancels alert subscriptions before bus disconnect.
- Add integration tests for health and live session injection.

Decision gate:

- /api/health.alert_subscribers is stable and heartbeat health remains
  unchanged.

ETA: 1 day.

Phase 5 - Suppression, rate-limit, dedup hardening

Deliverables:

- Finalize per-session/source rate storage.
- Add per-subscription min_interval and dedup TTL.
- Add drop telemetry.
- Add tests for duplicate, busy, tool-active, auto-continuing, and rate-limited
  cases.
- Add kill switch integration test.

Decision gate:

- No test path may enqueue stale alerts. Every suppression path must leave
  input_queue and chat_history unchanged.

ETA: 1-1.5 days.

Phase 6 - E2E, docs, and operational rollout

Deliverables:

- Add docs for daemon.alert_subscriber config, alerts.yaml, env overrides,
  payload contract, health fields, and at-most-once semantics.
- Add smoke/e2e test with fake or local NATS.
- Add rollout checklist:
  1. Deploy with enabled=false.
  2. Verify health shows configured_count and subscribed_count=0.
  3. Enable polymarket in staging with low rate limit.
  4. Publish synthetic alert.
  5. Verify one prompt injection and no duplicate HumanMessage.
  6. Enable production subscription.

Decision gate:

- Production enablement requires demonstrated health visibility, kill switch,
  and one successful synthetic alert in staging.

ETA: 1 day.

Realistic Total ETA
===================

Best case: 6 engineering days.

Expected: 7-8 engineering days including review, test stabilization, and
operational docs.

Risk buffer: 9-10 engineering days if EventInjector extraction exposes hidden
heartbeat assumptions or if alerts.yaml requires dependency/build changes.

Recommended Implementation Order
================================

The highest-risk part is not NATS subscription; it is avoiding a second prompt
injection mechanism. Implement EventInjector first and prove heartbeat still
works. Then add AlertSubscriber behind disabled config. This keeps the system
reversible: if alert-specific work slips, heartbeat remains stable and the
shared injector is still valuable.

Open Decisions
==============

1. Should default alert target_session be "kai" or a new dedicated
   "alerts" session?
   - Recommendation: "kai" for the required first polymarket subscription and KAI visibility in v1, but make it configurable and easy to switch.

2. Should alert turns require auto_mode?
   - Recommendation: false for alerts, true only for heartbeat. Alerts are
     external wakeups; requiring auto mode would silently drop useful alerts for
     an idle KAI.

3. Should alerts.yaml be mandatory?
   - Recommendation: optional overlay. Keep agent-config.json as the source of
     defaults and env overrides as the emergency operational path.

4. Should malformed raw payloads be shown to KAI?
   - Recommendation: no. They should be counted and logged safely, not injected.

5. Should v1 use durable NATS JetStream or ack/retry?
   - Recommendation: no. At-most-once is consistent with heartbeat and safer
     for prompt injection. Durable replay can be a separate task if operators
     later require guaranteed alert processing.
