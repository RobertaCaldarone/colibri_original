import re
import os

def log_info(msg): print(f"[*] {msg}")
def log_ok(msg):   print(f"[+] {msg}")
def log_warn(msg): print(f"[!] {msg}")
def log_err(msg):  print(f"[✖] {msg}")



def estrai_timestamp(line):
    """Estrae il timestamp all'inizio della riga come float"""
    match = re.match(r'^(\d+\.\d+)', line)
    if match:
        return float(match.group(1))
    return None



def calcola_differenze(file_path):

    init_duration = None
    auth_duration = None

    with open(file_path, 'r') as f:
        init_started = False
        auth_started = False
        init_start = None
        init_end = None
        auth_start = None
        auth_end = None
        send_count = 0

        for line in f:
            ts = estrai_timestamp(line)
            if ts is None:
                continue

            # --- INIT step ---
            if not init_started and "queueing IKE_VENDOR task" in line:
                init_started = True
                init_start = ts
                continue

            if init_started and "sending packet: from 192.168.100.3[500] to 192.168.100.2[500]" in line:
                send_count += 1
                if send_count == 2:
                    init_duration = ts - init_start
                    init_started = False
                    continue

            # --- AUTH step ---
            if not auth_started and "received packet: from 192.168.100.2[500] to 192.168.100.3[500]" in line:
                auth_started = True
                auth_start = ts
                continue

            if auth_started and "sending packet: from 192.168.100.3[4500]" in line:
                auth_duration = ts - auth_start
                auth_started = False
                continue

    results = {
        "init_duration": round(init_duration, 6),
        "auth_duration": round(auth_duration, 6)
    }

    return results



def parse_benchmark_output(output: str):
    """
    Estrae init_duration e auth_duration dall'output del benchmark.
    
    output: stringa intera dell'output del container
    ritorna: dizionario con valori arrotondati a 6 cifre
    """
    # cerca Init Time

    ansi_escape = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
    init_time = None
    init_size = None
    auth_time = None
    auth_size = None

    bench_lines = []
    lines = output.splitlines()

    for line in lines:
        clean_line = ansi_escape.sub('', line).strip()

        if "Init Time" in clean_line:
            match = re.search(r"Init Time:\s*([\d.]+)s", clean_line)
            if match:
                init_time = float(match.group(1))

            elif "Init Size" in clean_line:
                match = re.search(r"Init Size:\s*(\d+)", clean_line)
                if match:
                    init_size = int(match.group(1))

        if "Auth Time" in clean_line:
            match = re.search(r"Auth Time:\s*([\d.]+)s", clean_line)
            if match:
                auth_time = float(match.group(1))
        
        if "Handshake time" in clean_line:
            match = re.search(r"Handshake time:\s*([\d.]+)\s*seconds", clean_line)
            if match:
                total_time = float(match.group(1))


    print(f"Init time {init_time}")
    print(f"Auth time {auth_time}")

    results = {
        "init_duration": round(init_time, 6) if init_time else None,
        "auth_duration": round(auth_time, 6) if auth_time else None,
        "total_duration": round(total_time, 6) if total_time else None,
    }
    
    return results
