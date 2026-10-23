---
name: heavy-job-admission
description: Serialize heavy jobs with host memory preflight.
version: 1.0.0
author: Ben (@gadgethd), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [memory, resources, jobs, oom]
    category: devops
---

# Heavy Job Admission Skill

Use the Hermes resource guard to admit foreground jobs with a declared peak
RAM estimate. Jobs over 2 GiB share one slot across this OS user's profiles.

## When to Use

Use before local spatial fits, emulator/model rounds, large builds, or other
jobs expected to use several GiB. A 3–6 GiB job needs its own slot for its
whole lifetime, including model loading and any worker processes.

## Prerequisites

The execution host runs Linux with readable memory telemetry and this version
of Hermes installed. Use `terminal` on the machine that actually runs the job;
a guard on the control host cannot budget a remote backend.

## How to Run

Use `terminal` to inspect health:

```sh
hermes resources status
```

Admit a job, estimating peak memory for its complete process tree:

```sh
hermes resources run --memory-gib 6 -- python fit.py
```

For a bounded wait when another heavy job owns the slot:

```sh
hermes resources run --memory-gib 6 --wait-seconds 300 -- python fit.py
```

## Quick Reference

| Result | Meaning |
| --- | --- |
| Status exit 0 | Current pressure is ok; launch still needs admission. |
| Status exit 1 | Elevated or critical memory pressure. |
| Status exit 2 | Memory telemetry is unavailable. |
| Run exit 75 | Slot busy/unavailable or insufficient memory headroom. |
| Other run exit | The job's own exit status, or an execution error. |

## Procedure

1. Estimate the complete job's peak resident RAM, including child workers.
2. Run it through `hermes resources run`. Default contention behavior refuses
   immediately; use a finite wait when queued execution is appropriate.
3. Keep the process in the foreground inside the guard. For asynchronous
   terminal use, background the complete guarded command and retain its process
   handle with `notify_on_complete=true`.
4. On exit 75, inspect the reason. Wait for the running job to finish or for
   memory pressure to clear; avoid parallel retries or a smaller fake estimate.

The preflight leaves at least max(2 GiB, 15% of host RAM) available after the
estimated allocation. Swap does not count as job capacity. Memory is checked
after acquiring the slot, including after a wait.

## Pitfalls

- Admission is cooperative, with no hard memory cap. Unguarded processes and
  underestimated allocations can still exhaust RAM.
- Do not daemonize, detach, or dispatch work to another machine from inside the
  guarded command. Keep the owning foreground process alive until all work ends.
- Never delete the lock file to bypass contention. The kernel releases ownership
  when the job exits, including a killed job; a remaining file is harmless.
- Profiles share a slot because they share RAM. Separate OS users, containers
  with different lock files, and remote hosts need their own coordination.
- For Python tools, use `hermes_cli.heavy_jobs.heavy_job(estimated_bytes)` around
  the entire operation, off the event loop. Wait for children before exiting it.

## Verification

Check the guarded command's exit status with `terminal` or `process`. After it
finishes, inspect fresh health with `hermes resources status`. Preserve failures
and interrupted browser work for diagnosis; admission alone does not prove a
completed job or fix host earlyoom configuration.
