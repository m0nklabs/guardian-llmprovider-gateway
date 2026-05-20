# AI Server Upgrade Plan

Prepared: 2026-05-20
Status: Planning
Source: normalized from a decoded Dutch planning note.

## Overview

This upgrade plan aims to move the current host from an X99-era bottleneck to a more capable dual-socket AI workstation while reusing as much of the existing hardware as practical.

The target outcome is:

- A dual-socket LGA3647 platform with enough PCIe bandwidth for three GPUs.
- Reuse of the current DDR4 ECC/server memory, case, and initial GPU set.
- A phased rollout so the machine can be upgraded without buying everything at once.
- A final configuration with 44 CPU cores / 88 threads and 44 GB total VRAM.

## New Components To Buy

| Component | Recommendation | Rationale | Estimated Cost |
| --- | --- | --- | --- |
| Motherboard | Huananzhi X11D 16D | Dual-socket LGA3647 / C621 board with 4 real PCIe 3.0 x16 slots and 16 DDR4 slots | EUR 378.63 |
| CPUs | 2x Intel Xeon Gold 6152 (used) | 44 cores / 88 threads total; the second CPU is needed to unlock the full PCIe and memory topology | EUR 55.50 total |
| CPU coolers | 2x LGA3647 Narrow ILM coolers | Narrow ILM coolers for the Xeon platform, for example Snowman units | EUR 65.00 total |

## Existing Components To Reuse

| Component | Current Hardware | Plan |
| --- | --- | --- |
| Memory | 128 GB DDR4 server RAM (4x 32 GB) | Install 2 DIMMs on CPU 1 and 2 DIMMs on CPU 2 for dual-channel operation per processor |
| Case | Montech AIR 903 Base | Compatible with E-ATX, GPU length up to 400 mm, and CPU coolers up to 180 mm |
| GPUs | 1x RTX 3060 and 1x RTX 5060 Ti 16 GB | Keep both cards for Phase 1 |
| PSU | Cooler Master MWE Gold 850 V3 | Reuse in Phase 1 only |

## Fit And Build Notes

- The Montech AIR 903 Base should physically fit the planned motherboard and coolers.
- Cable routing may become tighter because the board can partially overlap the case cable cutouts.
- The second CPU is not optional if the full PCIe slot count and RAM slot availability are required.

## Phased Upgrade Plan

### Phase 1: Immediate Build With The Existing 850 W PSU

Use the current power supply to get onto the new platform first.

- Install the Huananzhi X11D 16D board.
- Install both Xeon Gold 6152 CPUs.
- Install both LGA3647 Narrow ILM coolers.
- Reinstall the existing 128 GB RAM kit.
- Reuse the current Cooler Master MWE Gold 850 V3 PSU.
- Reconnect the RTX 3060 via the normal PCIe power cable.
- Reconnect the RTX 5060 Ti 16 GB via its 12V-2x6 / 12VHPWR cable.

This phase is intended to get the new compute platform online without waiting for the final power and GPU expansion.

### Phase 2: One Month Later

Complete the final AI-focused expansion after the base platform is already stable.

- Add a third GPU by buying a second RTX 5060 Ti 16 GB.
- Replace the temporary Phase 1 PSU with a 1200 W ATX 3.1 power supply.
- Ensure each GPU has its own dedicated PCIe power path rather than sharing cables.

After Phase 2, the expected GPU memory pool is:

- RTX 3060: 12 GB
- RTX 5060 Ti: 16 GB
- Second RTX 5060 Ti: 16 GB
- Total VRAM: 44 GB

## Budget Summary

Estimated Phase 1 hardware cost:

- Motherboard: EUR 378.63
- 2x Xeon Gold 6152: EUR 55.50
- 2x LGA3647 coolers: EUR 65.00
- Phase 1 subtotal: about EUR 499.13

Phase 2 adds:

- One additional RTX 5060 Ti 16 GB
- One 1200 W ATX 3.1 PSU

## Target Result

If executed as planned, this upgrade delivers a strong value-focused local AI workstation with:

- 44 CPU cores / 88 threads
- 128 GB RAM reused from the current build
- 44 GB total VRAM after the final GPU expansion
- More usable PCIe bandwidth than the current X99 platform
- A staged budget path instead of a single all-at-once purchase

This is effectively the intended sweet spot for maximizing local AI compute per euro while reusing the expensive parts that still make sense.
