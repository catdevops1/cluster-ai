# A pod that restarted 3,720 times and never alerted

**TL;DR:** A DaemonSet crash-looped every ~6 minutes for 16 days on a production node. It produced no logs, reported `Ready: True`, and was invisible to restart-based alerting. Root cause was three layers below Kubernetes: an HWE kernel upgrade orphaned a legacy NVIDIA driver that could no longer build.

---

## What I found

Routine `kubectl get pods -A -o wide` on a 5-node bare-metal cluster:

```
kube-system  nvidia-device-plugin-daemonset-4rvck  1/1  Running  3720 (15d ago)  102d  node04
kube-system  nvidia-device-plugin-daemonset-5kmhc  1/1  Running  3     (77d ago)  102d  node02
kube-system  nvidia-device-plugin-daemonset-hg5px  1/1  Running  0                102d  node03
kube-system  nvidia-device-plugin-daemonset-xbj2t  1/1  Running  1     (78d ago)  102d  node01
```

Same DaemonSet, four nodes. One of them restarting roughly every six minutes, continuously, for over two weeks. The other three were fine.

## Why it was invisible

Three separate things hid this.

**No logs.** `kubectl logs --previous` returned nothing at all:

```
$ kubectl logs -n kube-system nvidia-device-plugin-daemonset-4rvck --previous --tail=30
$
```

Empty output from a pod with thousands of restarts is itself the clue — the container was failing *before* it started. There was no process to write anything.

**The pod reported healthy.** `kubectl describe` showed:

```
Ready:          True
Restart Count:  3720
```

`Ready: True` on a container that had never successfully started. Nothing watching pod readiness would flag this.

**My alerting filtered it out by design.** My monitoring agent uses delta-based restart tracking — it only alerts when a pod gains 3+ new restarts within a 5-minute window. That design exists to suppress the hundreds of cumulative restarts that system pods accumulate over a long-lived cluster. It works well. But a pod restarting steadily every six minutes never produces a spike, so it never crossed the threshold. Correct logic, wrong assumption: I had built for noisy history, not for slow sustained failure.

## Root cause

`kubectl describe` had the answer in `Last State`:

```
Reason:   StartError
Exit Code: 128
Message:  failed to create containerd task: failed to create shim task:
          OCI runtime create failed: could not apply required modification
          to OCI specification: error modifying OCI spec: failed to create
          the automatic CDI modifier: failed to generate CDI spec for mode
          "auto": failed to construct device spec generators:
          failed to initialize NVML: Driver Not Loaded
```

The failure was at OCI spec creation — the NVIDIA container runtime hook couldn't initialise NVML because the kernel driver wasn't loaded. That's why there were no logs: containerd never got as far as starting a container.

On the node:

```
$ nvidia-smi
NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.

$ lsmod | grep nvidia
$ dkms status
nvidia/390.157: added
$ uname -r
6.8.12-060812-generic
```

`added` — not `installed`. DKMS knew about the driver but had never built it for the running kernel. And it never could: the 390 legacy branch (required for the Kepler-era GTX 780M in that node) doesn't support kernel 6.x.

The chain, from `/var/log/apt/history.log`:

```
Upgrade: linux-generic-hwe-24.04 (6.17.0-40 → 7.0.0-28)
```

The HWE meta-package tracks newer kernels by design. A routine `apt upgrade` moved the kernel forward. The legacy driver couldn't build against it. The container runtime hook lost NVML. The device plugin started failing at OCI spec creation. Nothing alerted, because nothing was designed to notice a container that fails before it exists.

## Why only one node

`Node-Selectors: <none>` — the DaemonSet targeted every node. But only node04 had containerd configured with the NVIDIA runtime, so only node04 routed the container through the hook that failed. The other three used `runc` and started normally, doing nothing useful.

## Resolution

The GPU was never usable in the first place. The GTX 780M is CUDA compute capability 3.0; llama.cpp's CUDA backend requires 5.0+. Local inference had been running CPU-only for months regardless.
So:

1. Deleted the DaemonSet — it was applied manually, outside GitOps, with no Argo CD Application managing it
2. Verified `default_runtime_name = "runc"` in containerd, so no other workload depended on the hook
3. Purged the driver, DKMS entry, and `nvidia-container-toolkit` (~1 GB reclaimed)
4. Validated the containerd config parsed cleanly rather than restarting the runtime unnecessarily
5. Confirmed all 13 pods on node04 stayed Running throughout

## What I changed about monitoring

The interesting failure here wasn't the driver. It was that a component failed continuously for 16 days and my monitoring — which watches compute, storage, certificates, Vault, GitOps health, and external endpoints — didn't say a word.

Two detection gaps, both of the same shape: **slow, silent stalls rather than spikes.**

**`SustainedCrashLoop`** — tracks a second, hour-long window alongside the 5-minute one. If a pod gains 6+ restarts over an hour, it alerts, even when no single 5-minute delta crosses the threshold. The nvidia plugin would have gained ~10 per hour.

**`PodStuckPending`** — flags any pod in `Pending` for more than 5 minutes. Pods that never start accumulate zero restarts, so restart-based checks are blind to them entirely. This catches RWO multi-attach deadlocks, image pull failures, and unschedulable pods.

Delta-based alerting answers "did something just break?" It doesn't answer "has something been broken this whole time?" Those need different queries. Both checks are in [`backend/monitor.py`](../backend/monitor.py).

## Takeaways

- **`Ready: True` is not health.** A container can fail at OCI spec creation and still leave a pod reporting ready.
- **Empty logs on a crash-looping pod is a signal**, not a dead end — it localises the failure to before container start.
- **Meta-packages move kernels.** Anything depending on an out-of-tree kernel module is one `apt upgrade` from breaking, silently, until reboot.
- **Anything outside GitOps will be forgotten.** This DaemonSet was applied by hand 102 days earlier and nothing in Git described it, so nothing reconciled it and nobody revisited it.
- **Alert thresholds encode assumptions.** Mine assumed failures announce themselves. This one didn't.
