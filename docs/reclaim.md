# Reclaiming space

`rig disk` says a filesystem is filling. `tools/rig-reclaim` says what to delete
first.

```
tools/rig-reclaim /mnt/archive4
tools/rig-reclaim / --min-gib 5 --top 10
tools/rig-reclaim /mnt/archive4 --json
```

It reports and never deletes. Removal is a human decision, and this tool has no
way to know which of your projects is dead.

## What it looks for

Directories a build system will recreate on demand. Each one is ranked by what
you pay to get it back, cheapest first:

| Category | Matches | Cost to restore |
| --- | --- | --- |
| `trash` | `.Trash-1000` | Nothing. The files are already deleted. |
| `python` | `.venv`, `venv`, `__pycache__` | `pip install`, needs network |
| `pkgcache` | `.pnpm-store`, `.gradle`, `.ccache` | Refetched on the next build |
| `node` | `node_modules` | Package install, needs network |
| `rust` | `target` beside a `Cargo.toml` | `cargo build` |
| `unity` | `Library`, `Temp`, `Logs`, `Obj` beside `Assets`+`ProjectSettings` | Editor reimport, minutes to hours |
| `unreal` | `Intermediate`, `DerivedDataCache`, `Saved`, `Binaries` | Rebuild, can be hours |

Sizes come from `st_blocks`, so they are the bytes actually on the drive. On a
compressed btrfs that is well below the apparent size, and it is the number that
decides whether the filesystem stops being full.

Any candidate whose sibling source files have not been touched in 60 days is
marked `STALE`. That is the column to sort your attention by — a 40 GiB `target`
under a project last edited five months ago costs a rebuild you were never going
to run anyway.

## Two traps it already handles

**`venv` is not always a virtualenv.** Blender and Nuke ship the Python standard
library, and the stdlib contains a module directory literally named `venv`.
Deleting it breaks the application's bundled Python and frees about 60 KB. Only
a directory carrying `pyvenv.cfg` is a real virtualenv, and only those are
reported.

**`target` is not always Rust.** Plenty of trees contain a directory called
`target` that is not a Cargo build. Only one sitting beside a `Cargo.toml` is
reported.

## Two traps it does not

**An installed engine looks exactly like a build cache.**
`UNREAL/UE_*/Engine/Binaries` and `Engine/Intermediate` match the `unreal`
rules and can be tens of gigabytes each, but they are a compiled engine, not a
project artifact. Deleting them means rebuilding Unreal from source. Read the
paths before acting on the category total.

**A `target` inside a git worktree is still build output, but the worktree
around it may hold uncommitted work.** Check with
`git -C <worktree> status --porcelain` before removing the worktree itself, and
prefer `git worktree remove` over `rm -rf` so the parent repo's metadata stays
consistent.

## btrfs frees files, not chunks

Deleting files returns space to the filesystem. It does not return allocated
chunks to the unallocated pool, so `btrfs filesystem usage` can still show
allocation near capacity right after a large delete. Read the ratio:

```
cat /sys/fs/btrfs/<uuid>/allocation/data/bytes_used
cat /sys/fs/btrfs/<uuid>/allocation/data/total_bytes
```

If chunks are heavily allocated but lightly used, a filtered balance
(`btrfs balance start -dusage=50 <mount>`) compacts them. Run it when the
machine is otherwise idle — it rewrites extents and will itself show up in
`rig disk` as sustained IO.
