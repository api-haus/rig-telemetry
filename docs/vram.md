# VRAM

System RAM has an arbitration layer. When it runs out, the kernel scores every
process and kills the largest. VRAM had none: the driver refused the **next**
caller, which is usually the compositor asking for a few MB to redraw a window,
not the client that filled the card.

`tools/vram-guard` adds the arbitration layer.

```
sudo tools/vram-guard install     # boot-time drop-in, and Resizable BAR
sudo tools/vram-guard apply 1000  # the same, for the session already running
tools/vram-guard status           # what is delegated, what is capped
sudo tools/vram-guard verify      # prove the cap still holds
```

`status` exits 1 when `app.slice` carries no cap, so a monitor can call it.

## Why the driver does not do this on its own

Three separate reasons, and none of them is a local misconfiguration.

**There is no overcommit.** Windows pages GPU memory to system RAM through
WDDM, so an allocation gets slow instead of failing. AMD and Intel do the
equivalent on Linux through TTM, which evicts to system RAM under pressure. The
NVIDIA Linux driver does neither for graphics allocations. A full card is a hard
`NV_ERR_NO_MEMORY`.

**The refusal cannot be aimed.** An allocator has no concept of guilt. The
client that already holds 12.5 GiB keeps running, because it is not asking for
anything. Whoever asks next is refused. Electron treats that as fatal and raises
`SIGTRAP`; a terminal that needs a GPU context cannot start at all.

**The kernel mechanism exists but nothing turns it on.** Linux 6.14 added the
cgroup v2 `dmem` controller for exactly this. systemd has no directive for it
and does not propagate it — the [pull
request](https://github.com/systemd/systemd/pull/37079) is still a draft,
because the maintainers judge the kernel side to be in flux. So every
`dmem.max` on a stock machine reads `max`.

## Where the cap sits

uwsm already separates the two populations:

```
user@1000.service
├── session.slice   compositor, portals, pipewire, dbus-broker   uncapped
├── app.slice       every desktop application, games included    capped
└── background.slice
```

The cap goes on `app.slice`. Applications are refused together once they reach
it; the compositor keeps the remainder and survives to show the failure.

The default reserve for `session.slice` is 2.5 GiB, against a measured 0.23 GiB
in use with the compositor, the portals, a shell and a wallpaper engine
running. `--reserve` changes it. On a 15.63 GiB region that leaves applications
13.13 GiB.

## What is proved, and how

`vram-guard verify` allocates through the CUDA driver API inside a scratch
cgroup until refused. Three cases, all passing on driver 610.43.03:

| Case | Cap | Refused after | Charged |
| --- | --- | --- | --- |
| cap on the cgroup itself | 1.00 GiB | 640 MiB | 897 MiB |
| cap on the cgroup itself | 2.00 GiB | 1664 MiB | 1921 MiB |
| cap inherited from the parent | 1.00 GiB | 640 MiB | 897 MiB |
| no cap | — | not refused at 6144 MiB | 6400 MiB |

The refusal point tracks the cap, and the last row shows the same probe running
to the test ceiling when nothing limits it. The third row is the one the design
rests on, because applications sit in slices **below** `app.slice`.

The charge is not a CUDA-only path. It is taken in `vidmemConstruct_IMPL`,
which constructs every video memory allocation the resource manager makes,
whatever API asked for it.

## Limits worth knowing

**Accounting is not retroactive.** A charge attaches to the cgroup that existed
when the allocation was made. `apply` on a running session therefore starts
counting from zero while the applications already hold memory, and the cap
allows that much again on top. It is exact from the next login, where the
drop-in runs before any application starts.

**Only `dmem.max` is enforced.** `dmem.min` and `dmem.low` are eviction hints
for drivers that can evict. The NVIDIA module references
`dmem_cgroup_try_charge` and `dmem_cgroup_uncharge` and none of the eviction
helpers, so a reservation cannot be expressed — the cap on the applications is
the whole mechanism.

**Processes outside `app.slice` are uncapped.** A GPU job started over SSH lands
in the login session scope, not in the user manager. Route those through the
`gpu` processqueue, which is what it is for.

**`app.slice` has to exist.** systemd keeps a slice for the life of the user
manager, and the drop-in recreates it if it is missing. `status` reports
`absent` if this ever stops being true.

## BAR1

A Base Address Register is an address window the CPU reaches a PCI device
through. BAR1 is the one that reaches video memory. It is a window onto VRAM,
not VRAM itself: the card holds 16 GiB, and by default the CPU can see 256 MiB
of it at a time.

Anything the CPU must touch needs a slot in that window — buffer uploads, mapped
buffers, the compositor's imported buffers. The driver keeps a mapping database
and recycles slots. When the window has no free address space, the driver cannot
create the next mapping and fails, which is the
`reusemappingdbMap(&pBar1VaInfo->reuseDb, ...)` assert at `kern_bus_gm107.c:3151`.
This is a second ceiling and a much lower one, so a card can refuse work while
the frame buffer still reads 80 percent full.

Resizable BAR widens BAR1 to cover the whole frame buffer. It does not add
memory and it does not stop VRAM exhaustion. It removes the lower of the two
ceilings, so the only way left to refuse an allocation is the one the cap
arbitrates.

Two settings have to agree.

**Driver.** `NVreg_EnableResizableBar` has defaulted to 0 since the option
appeared in 530.30.02. `install` writes
`/etc/modprobe.d/50-rig-nvidia-resizable-bar.conf` and rebuilds the initramfs,
which the `modconf` hook copies `modprobe.d` into. To revert, delete that file,
run `mkinitcpio -P`, and reboot. `--no-resizable-bar` skips it at install time.

**Firmware.** The BAR has to fit inside the prefetchable window the firmware
gave the GPU's root port. `Above 4G Decoding` alone is not enough: it publishes
a large window on the host bridge, but the root port keeps a small one below
4 GiB until `Re-Size BAR Support` is also on. `status` reads both and says so:

```
BAR1        0.25 GiB now, 16.00 GiB supported by the card
firmware    0.56 GiB prefetchable window on the root port
            too small for a resized BAR — enable Re-Size BAR
            Support in firmware, then reboot
```

A 0.56 GiB window cannot hold a 16 GiB BAR, so the driver would fall back to
256 MiB whatever the module option says. Both lines have to be satisfied before
`nvidia-smi -q -d MEMORY` reports a BAR1 total larger than 256 MiB.

## Telemetry

`RigVRAMExhausted` fires above `rig:gpu:vram_used_ratio` 0.85. The ratio counts
from `free`, not `used`, because `nvidia-smi` excludes the driver reserve from
`used`. A 0.95 threshold would never have fired for the incident that caused
this page.
