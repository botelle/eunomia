#!/usr/bin/env python3
"""Compare Apple `container` vs colima/docker on one identical image.

One variable: the runtime. Same image digest, same host, same port shape,
alternating runtimes so drift in host load hits both arms equally.
"""
import json, os, socket, subprocess, sys, time

IMAGE = "nginx:alpine"
APPLE_IMAGE = "docker.io/library/nginx:alpine"
TRIALS = 5
ENV = dict(os.environ)
ENV["PATH"] = "/opt/homebrew/bin:" + ENV.get("PATH", "")
ENV["DOCKER_HOST"] = "unix:///Users/operator/.colima/default/docker.sock"


def sh(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, env=ENV, timeout=timeout)


def ready(port, deadline):
    """Poll until the port answers a real HTTP 200. Returns seconds, or None."""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25) as s:
                s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
                if b"200" in s.recv(64):
                    return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


def bench(name, port, run_cmd, stop_cmd, rm_cmd, start_cmd):
    cold, warm = [], []
    for i in range(TRIALS):
        sh(stop_cmd); sh(rm_cmd)                     # ensure clean
        t0 = time.monotonic()
        r = sh(run_cmd)
        if r.returncode != 0:
            print(f"  {name} trial {i}: RUN FAILED: {r.stderr.strip()[:200]}")
            continue
        ok = ready(port, t0 + 60)
        cold.append(time.monotonic() - t0 if ok else None)

        # warm restart: same container, stop then start
        sh(stop_cmd)
        t1 = time.monotonic()
        r = sh(start_cmd)
        ok = ready(port, t1 + 60) if r.returncode == 0 else False
        warm.append(time.monotonic() - t1 if ok else None)

        sh(stop_cmd); sh(rm_cmd)
    return cold, warm


def mem(patterns):
    """Sum RSS (MiB) of processes whose command matches any pattern."""
    out = sh(["ps", "-axo", "rss=,command="]).stdout
    total = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        rss, _, cmd = line.partition(" ")
        if any(p in cmd for p in patterns):
            try:
                total += int(rss)
            except ValueError:
                pass
    return round(total / 1024, 1)


APPLE_PATS = ["container-apiserver", "container-runtime-linux", "container-core-images",
              "container-network-vmnet"]
COLIMA_PATS = ["colima", "limactl", "qemu-system", "socket_vmnet"]

print("=== idle memory (before any container) ===")
print(f"apple  runtime procs: {mem(APPLE_PATS)} MiB")
print(f"colima runtime procs: {mem(COLIMA_PATS)} MiB")

print("\n=== apple container ===")
a_cold, a_warm = bench(
    "apple", 8011,
    ["container", "run", "-d", "--name", "bench-a", "-p", "127.0.0.1:8011:80", APPLE_IMAGE],
    ["container", "stop", "bench-a"], ["container", "delete", "bench-a"],
    ["container", "start", "bench-a"])

print("=== colima / docker ===")
c_cold, c_warm = bench(
    "colima", 8012,
    ["docker", "run", "-d", "--name", "bench-c", "-p", "127.0.0.1:8012:80", IMAGE],
    ["docker", "stop", "bench-c"], ["docker", "rm", "-f", "bench-c"],
    ["docker", "start", "bench-c"])

print("\n=== memory with one container running ===")
sh(["container", "run", "-d", "--name", "memtest-a", "-p", "127.0.0.1:8011:80", APPLE_IMAGE])
ready(8011, time.monotonic() + 30)
print(f"apple  + 1 container: {mem(APPLE_PATS)} MiB")
sh(["container", "stop", "memtest-a"]); sh(["container", "delete", "memtest-a"])

sh(["docker", "run", "-d", "--name", "memtest-c", "-p", "127.0.0.1:8012:80", IMAGE])
ready(8012, time.monotonic() + 30)
print(f"colima + 1 container: {mem(COLIMA_PATS)} MiB")
sh(["docker", "stop", "memtest-c"]); sh(["docker", "rm", "-f", "memtest-c"])

def fmt(xs):
    got = [x for x in xs if x is not None]
    if not got:
        return "all failed"
    got.sort()
    return (f"median {got[len(got)//2]:.2f}s  min {got[0]:.2f}s  max {got[-1]:.2f}s"
            f"  (n={len(got)}/{len(xs)})")

print("\n=== RESULTS (seconds to first HTTP 200) ===")
print(f"apple  cold: {fmt(a_cold)}")
print(f"colima cold: {fmt(c_cold)}")
print(f"apple  warm: {fmt(a_warm)}")
print(f"colima warm: {fmt(c_warm)}")
print("\nraw:", json.dumps({"apple_cold": a_cold, "colima_cold": c_cold,
                            "apple_warm": a_warm, "colima_warm": c_warm}))
