# Capability-Kernel Port Plan

This document is the Hermes-owned adaptation guide for the capability-plugin
kernel released in TaskChad OS v1.7.0. It exists so `hermes-talk` and future
Hermes plugins can reuse the hard-earned lifecycle and safety practices without
copying another runtime's implementation or weakening Hermes's host boundary.

This is a design and acceptance contract. `hermes-talk` does **not** currently
claim hot plugin activation, transactional unload, or restart recovery.

## What already exists here

`hermes-talk` already has useful pieces of the target contract:

- `plugin.yaml` declares the package and hooks through Hermes's native plugin
  surface.
- `register(ctx)` and `PluginContext` are the host-owned integration seam.
- `talk_capabilities` reports a bounded read-only catalog, preferring attached
  in-process truth and falling back to the authenticated API server.
- `talk_status` and `hermes talk doctor --json` distinguish configuration and
  runtime lanes without printing credentials.
- Tool execution stays behind Hermes dispatch and Talk's operator authority
  ledger; catalog visibility grants nothing.
- Plugin updates honestly require a gateway restart because a running process
  keeps executing the code it loaded at startup.

Those are foundations, not proof of the lifecycle kernel described below.

## Ported invariants

### 1. Declaration before execution

Parse a bounded, closed declaration before importing plugin code. The parser
must reject unknown fields, unsupported versions, duplicate contribution IDs,
cycles, invalid replacements, excessive size/depth, and paths that escape the
approved package root.

Hermes owns the declaration format. A future schema may extend `plugin.yaml`
or use a separately versioned contribution document, but it must not silently
reinterpret existing manifest v1 packages as capability plugins.

### 2. Accepted bytes are executed bytes

Discovery should capture a content-addressed artifact set. Activation must use
that accepted set or prove an equivalent immutable package identity. Never
validate one file and later import whatever happens to occupy the path.

### 3. Reach is not authority

Contribution registration makes a tool reachable; it does not make the tool
allowed. Hermes tool policy, session scope, Talk's authority ledger, and
operator confirmation continue to decide whether a call executes. Plugin
metadata cannot widen those grants.

### 4. Publish complete snapshots

Build candidate registries away from active readers. A host-owned publication
boundary swaps one immutable complete snapshot for another. No turn may see a
half-registered plugin.

For the first delivery, gateway startup is the publication boundary. Hot
reload stays out of scope until Hermes exposes a safe turn boundary and owns
the registry swap.

### 5. Dispose in reverse order

Every activated contribution returns a disposer. Unload follows reverse
dependency order. If cleanup cannot be proved, report `restart_required`; do
not claim the plugin is unloaded merely because it disappeared from a catalog.

### 6. Journal intent and result

Lifecycle changes write bounded, redacted receipts before and after mutation.
Recovery must distinguish committed, rolled-back, and uncertain transitions
without replaying external actions. Credentials, environment values, absolute
private paths, logs, and arbitrary exception text do not belong in receipts.

### 7. Preserve lane truth

Keep these states separate in doctor/status output:

1. package installed;
2. plugin enabled in configuration;
3. declaration accepted;
4. code loaded by this process;
5. contributions published to this registry snapshot;
6. a live call executed through the expected authority path.

An updated file on disk proves only the first few layers. It does not prove the
running gateway changed.

## Hermes-specific boundaries

- Keep plugin discovery and registration behind Hermes core APIs. Talk should
  consume the resulting catalog, not become a second plugin manager.
- Keep `talk_capabilities` read-only, bounded, and source-labelled.
- Freeze advertised Realtime tool schemas for a voice session. A lifecycle
  change becomes visible in the next minted session unless an explicit schema
  renegotiation protocol is added and tested.
- Preserve older-host degradation: missing lifecycle APIs produce a named
  unsupported/restart-required state, not guessed emulation.
- Do not add private TaskChad OS modules as dependencies. Reimplement the
  small contracts in Hermes terms and retain provenance in documentation.

## Delivery sequence

### Slice 1: strict declaration and fixtures

- Specify a versioned closed schema.
- Build an import-free parser and bounded artifact capture.
- Add hostile fixtures for unknown fields, cycles, conflicts, path escapes,
  symlink swaps, oversized documents, deep nesting, and artifact drift.

### Slice 2: read-only discovery

- Discover candidates without loading code.
- Add accepted/rejected records to a bounded host catalog.
- Surface them through doctor/status with stable error codes.

### Slice 3: cold activation

- Activate only accepted candidates at gateway startup.
- Register through `PluginContext`/Hermes registries.
- Prove contribution ownership and authority separation.
- Continue to require restart after update or disable.

### Slice 4: lifecycle receipts and recovery

- Journal intent/result receipts atomically.
- Recover interrupted transitions without replaying side effects.
- Add `restart_required` and operator remediation to doctor.

### Slice 5: optional hot lifecycle

Attempt this only after Hermes provides a host-owned turn boundary, immutable
registry snapshots, disposer ownership, and deterministic rollback. Voice
session schema behavior must be specified separately.

## Acceptance matrix

| Failure | Required result |
|---|---|
| Malformed/oversized declaration | Rejected before import |
| Path or symlink escape | Rejected with stable redacted code |
| Artifact changes after validation | Activation refused |
| Duplicate contribution/replacement conflict | Deterministic rejection |
| Dependency cycle | Deterministic rejection without recursion failure |
| Activation failure | Old complete snapshot remains active |
| Disposal failure | `restart_required`; no false unload claim |
| Interrupted journal transition | Deterministic recovery; no side-effect replay |
| Plugin declares a tool outside session grants | Tool remains denied |
| Update lands on disk while gateway runs | Doctor reports old loaded version |
| Voice session predates a plugin change | Existing schema remains frozen |
| Public package/export | No secrets, runtime journals, logs, or private state |

## Upstream reference

The public TaskChad OS manual is the behavioral source for these lessons:

- [Capability Plugin Kernel](https://github.com/TheSmokeDev/taskchad-os/blob/master/docs/manual/features/capability-plugin-kernel.md)
- [Hermes Talk Capability-Kernel Port](https://github.com/TheSmokeDev/taskchad-os/blob/master/docs/manual/features/hermes-talk-capability-port.md)

Use those documents as design evidence, not as an instruction to share source
files. Hermes remains the owner of its manifest, registry, lifecycle, and
operator surfaces.
