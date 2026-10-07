import subprocess
from utils.log import *

def _run(cmd):
    return subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)

def apply_netem(iface, delay_ms, jitter_ms=0):
    subprocess.run(f"sudo tc qdisc del dev {iface} root", shell=True, capture_output=True)
    if jitter_ms and jitter_ms > 0:
        cmd = f"sudo tc qdisc add dev {iface} root netem delay {delay_ms}ms {jitter_ms}ms distribution normal"
    else:
        cmd = f"sudo tc qdisc add dev {iface} root netem delay {delay_ms}ms"
    _run(cmd)
    log_ok(f"netem applicato su {iface}: delay={delay_ms}ms jitter={jitter_ms}ms")

def clear_netem(iface):
    subprocess.run(f"sudo tc qdisc del dev {iface} root", shell=True, capture_output=True)
    log_ok(f"netem rimosso da {iface}")

def measure_rtt(target_ip, count=20, source_container=None):
    if source_container:
        cmd = f"docker exec {source_container} ping -c {count} -i 0.2 {target_ip}"
    else:
        cmd = f"ping -c {count} -i 0.2 {target_ip}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        log_err(f"ping fallito (source={source_container or 'host'}) verso {target_ip}:\n{result.stdout}\n{result.stderr}")
        return False, None
    last_line = result.stdout.strip().splitlines()[-1]
    try:
        avg = float(last_line.split('/')[4])
    except Exception as e:
        log_err(f"parsing ping fallito: {e}")
        return False, None
    return True, avg

def verify_rtt(target_ip, expected_rtt_ms, tolerance_pct=15, min_tolerance_ms=0.45, count=50, source_container=None):
    ok, measured_avg = measure_rtt(target_ip, count=count, source_container=source_container)
    if not ok:
        return False, None
    tolerance_ms = max(expected_rtt_ms * tolerance_pct / 100, min_tolerance_ms)
    lower = expected_rtt_ms - tolerance_ms
    upper = expected_rtt_ms + tolerance_ms
    ok2 = lower <= measured_avg <= upper
    if ok2:
        log_ok(f"delay verificato: atteso={expected_rtt_ms}ms misurato={measured_avg}ms (range {lower:.2f}-{upper:.2f}ms)")
    else:
        log_err(f"VERIFICA FALLITA: atteso={expected_rtt_ms}ms misurato={measured_avg}ms — fuori range {lower:.2f}-{upper:.2f}ms")
    return ok2, measured_avg
