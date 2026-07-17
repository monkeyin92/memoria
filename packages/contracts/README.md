# Shared contracts

JSON Schema for LiveKit data-channel UI events shared by the agent and `apps/web`.

Clients **must** drop events with a smaller `generation_id` than the latest accepted one.
