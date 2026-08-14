---
name: rig-devenv
description: Attribute machine load to a specific project's development environment and cap it, once telemetry has named the culprit. Use when a docker compose stack, database, message broker, language server, file watcher, test runner or build toolchain is loading the machine; when several git worktrees each run their own copy of a stack; when a container is crash-looping or squatting in swap; or when the user asks to limit, cap, throttle, contain or shrink a dev environment. This is about the environment around the code — containers, daemons, toolchains, watchers — never about optimising the project's own source.
---

# rig-devenv

**Scope.** This skill contains a machine that is being made unusable by the
*scaffolding* around a project: containers, brokers, databases, watchers,
language servers, compilers, linkers. It never touches the project's own
algorithms, queries or architecture. If the user wants the application itself
made faster, this is the wrong skill — say so and stop.

Diagnosis comes first. If load has not yet been attributed, use **rig-diagnose**
and come back with a name.

## 1. Attribute it

```
rig containers                       # cost per compose stack
rig containers --each --sort swap    # per container
rig who --by cpu                     # per process group, non-container
rig who --by swap --top 15
```

Then find the source of the stack on disk:

```
docker inspect <container> --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

Read its compose file before proposing anything. Limits that contradict a
service's own configuration will fail at startup, not at review.

## 2. Measure before and after

An uncapped stack that is idle looks cheap in RSS and expensive nowhere —
because it has been evicted to swap. **Swap is where idle dev environments
hide.** Always record both:

```
rig containers --sort swap
rig q 'rig:stack:rss_bytes'
rig q 'rig:stack:swap_bytes'
rig q 'rig:stack:cpu_cores'
```

Write the numbers down before changing anything. Re-run them after. A change
you cannot show a number for did not happen.

## 3. Cap it

Never edit the project's committed compose file. Write an **overlay** the user
opts into:

```
docker compose -f docker-compose.yml -f docker-compose.limits.yml up -d
```

A template is in `references/compose-limits.yml` next to this skill.

### The load-bearing line

```yaml
mem_limit: 512m
memswap_limit: 512m     # equal to mem_limit, so: no swap at all
```

**`memswap_limit` is the whole point.** It is total memory + swap. Setting it
equal to `mem_limit` forbids the container from touching swap. Without it, a
container hits its memory cap and *spills into swap* instead of being
constrained — which is the exact failure being fixed: idle services parking
gigabytes in swap, then thrashing the disk to fault back in.

Verify the kernel supports it before promising it works:

```
cat /sys/fs/cgroup/system.slice/docker-*.scope/memory.swap.max | head -1
```

`max` means settable. Absent means cgroup v1 without swap accounting, and
`memswap_limit` will be silently ignored.

### Restart policy

`restart: unless-stopped` on a service whose dependency has died is an infinite
loop that burns CPU and IO forever. Check before leaving it alone:

```
for c in $(docker ps -aq); do docker inspect "$c" --format '{{.RestartCount}} {{.Name}}'; done | sort -rn | head
```

Four figures means a crash loop. Use `restart: on-failure:3` so a broken
service becomes a visible stopped container instead of silent load.

## 4. Per-runtime knobs that actually matter

A memory cap alone makes a runtime OOM rather than behave. Tell it to be small
as well as capping it.

| Runtime | Set | Why |
| --- | --- | --- |
| Any JVM (Kafka, Elasticsearch, Gradle) | `-XX:ActiveProcessorCount=2` plus `-Xmx` | Without it the JVM sizes GC and IO thread pools from the *host's* core count — per container. On a 24-thread box, five brokers each build for 24 CPUs. |
| Node / tsx / vite / webpack | `NODE_OPTIONS=--max-old-space-size=<mem_limit minus ~30%>` | V8 sizes its heap from host RAM and will not collect until far too late. |
| Postgres (throwaway dev DB) | `-c fsync=off -c synchronous_commit=off -c full_page_writes=off -c shared_buffers=128MB` | A database recreated by a migration command does not need crash safety; it costs only disk bandwidth. **Never on anything holding real data.** |
| Redis | `--save "" --maxmemory <cap> --maxmemory-policy allkeys-lru` | `--save ""` stops periodic RDB forks and background writes. |
| Kafka | small heap, `NUM_NETWORK_THREADS`/`NUM_IO_THREADS`/`BACKGROUND_THREADS` = 2, short `LOG_RETENTION_HOURS`, `LOG_CLEANER_ENABLE=false` | Defaults assume a production broker, not one test suite. |

## 5. Duplicate stacks are usually the largest single win

One stack per git worktree multiplies everything. Check for it explicitly:

```
docker ps -a --format '{{.Label "com.docker.compose.project"}}' | sort | uniq -c | sort -rn
```

Several projects differing only by branch name means N copies of the same
database, broker and cache are resident. Stopping the ones not in use beats any
amount of tuning. This is the user's call — present the measured cost per stack
and let them choose.

## 6. Build toolchains

Compilers and linkers are dev environment too, and they fail differently:
parallelism limits **job count**, not **memory per job**.

- A `-j N` cap still permits N concurrent linkers. If each holds gigabytes, the
  machine still runs out. Measure with
  `rig who --by mem --since 20m --agg max` during a build.
- For Rust, linker peak memory is dominated by debug info, not by `-j`.
  `[profile.dev] debug = "line-tables-only"` or `split-debuginfo = "unpacked"`
  cuts it far more than lowering `-j`.
- Language servers (rust-analyzer, tsserver, JetBrains indexers) are long-lived
  and index on every branch switch. They show under `rig who --by mem` as their
  own groups.
- On this machine, serialise contended jobs through `processqueue <queue> <cmd>`
  rather than adding more parallelism limits.

## 7. Verify, then report

```
docker compose -f <base> -f <overlay> config     # merge is valid, env merged not replaced
docker compose -f <base> -f <overlay> up -d
rig containers --sort swap                       # compare against step 2
```

Confirm the overlay **merged** rather than replaced. An `environment:` block in
an overlay merges key by key; a `command:` replaces wholesale. Check a variable
the base defined and the overlay did not:

```
docker compose -f <base> -f <overlay> config | grep -A3 DATABASE_URL
```

Report the before and after numbers and the hard ceiling now in force. Applying
the overlay restarts containers — that is the user's decision, not yours; give
them the command.

## Where things are

`rig where` prints the telemetry stack root on this machine; `rig where -q`
prints the path alone. Never hardcode it. Read `$(rig where -q)/AGENTS.md` for
the metric vocabulary, and `docs/metrics.md` there for `rig:stack:*` and
`rig:proc:*`. Dashboards at <http://localhost:13337>.

The compose template beside this skill is at
`${CLAUDE_PLUGIN_ROOT}/skills/rig-devenv/references/compose-limits.yml`.
