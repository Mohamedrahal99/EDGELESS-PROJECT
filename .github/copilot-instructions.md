# EDGELESS AI coding guide

## Repository shape
- This is a Rust workspace (`Cargo.toml`), not a single app. The important crates are `edgeless_function`, `edgeless_node`, `edgeless_orc`, `edgeless_con`, `edgeless_api`, `edgeless_dataplane`, `edgeless_cli`, and `edgeless_inabox`.
- The platform is organized around workflows, functions, and resources. `examples/` and `functions/` are the main “behavioral” code; service crates define the runtime, orchestration, and APIs.
- Most runtime-facing function code is WASM and implements `edgeless_function::EdgeFunction` (`handle_init`, `handle_cast`, `handle_call`, `handle_stop`). See `functions/noop/src/lib.rs` and `functions/convmem/src/lib.rs`.
- State and lifecycle are explicit: init payloads are parsed with `parse_init_payload`, and state can be serialized by a function and restored on restart.

## Architectural patterns
- Cross-component communication is mostly gRPC/protobuf at the API layer, while function-to-function interaction uses the EDGELESS event model (`cast` vs `call`) rather than direct Rust calls.
- `edgeless_function/src/lib.rs` is the key compatibility layer: it exposes the host API (`cast`, `sync`, `telemetry_log`, etc.) and the function contract for WASM modules.
- Examples are the best reference for how this codebase expects new logic to be structured; prefer matching the surrounding example’s resource and event usage instead of inventing a new pattern.
- The repo makes strong use of startup config (`init-payload`, `workflow.json`, generated `controller.toml`/`node.toml`/`orchestrator.toml`), so inspect example workflow JSON and config files before adding new runtime options.

## Build and debug workflow
- Normal workspace build: `cargo build`.
- Tests: `cargo test`.
- For function examples: `target/debug/edgeless_cli function build functions/<name>/function.json`.
- Local stack startup from README: `target/debug/edgeless_inabox -t` and `target/debug/edgeless_cli -t cli.toml`, then `target/debug/edgeless_inabox`.
- Set `RUST_LOG=info` when debugging runtime behavior (`README.md`), and check `log::info!`/`telemetry_log(...)` messages emitted by functions.
- Many example workflows require external services (Kafka, Redis, Ollama, HTTP endpoints); use the per-example README before assuming a generic run path.

## Project-specific conventions
- Prefer small, explicit Rust APIs over heavy abstraction. The codebase values straightforward host/resource boundaries and serialization for persistence.
- If a function needs to persist state, follow the pattern of storing a compact serialized snapshot and restoring it in `handle_init`.
- For messaging, model semantics explicitly: `cast` is fire-and-forget; `call` expects a reply contract (`CallRet`). Do not improvise a different lifecycle without checking an existing example.
- For new init options, parse them with the same style as `parse_init_payload("key=value,...")` and read them in `handle_init`.
- When touching cross-crate APIs or message schemas, update both producers and consumers; this repo frequently couples gRPC schemas and runtime behavior.

## Good starting points
- `README.md` for system overview and startup commands.
- `BUILDING.md` for toolchain and environment requirements.
- `documentation/repository_layout.md` for crate responsibilities.
- `edgeless_function/src/lib.rs` for the WASM host contract.
- `functions/README.md` and `examples/README.md` for end-to-end usage patterns.
- `functions/convmem/src/lib.rs` for a more advanced stateful function with timeout/watchdog logic and telemetry.
