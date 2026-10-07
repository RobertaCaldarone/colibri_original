from utils.docker import ( is_docker_running, start_docker_linux, docker_compose_up, docker_compose_down, get_veth, exec_in_container)
from utils.monitoring import (get_mem_usage, monitor_container_resources)
from utils.save import (save_benchmark_results)
from utils.log import *
from utils.network import apply_netem, clear_netem, verify_rtt, measure_rtt
import yaml
import subprocess
import threading
import time
import os
import re
import statistics
import argparse

CONF_FILE = "config.yml"

all_results = []
timings_list = []

def run_single_iteration(container_name, command):
    
    stop_event = threading.Event()
    result_holder = [] 

    monitor_thread = threading.Thread(
        target=monitor_container_resources,
        args=(container_name, stop_event, result_holder),
    )
    monitor_thread.start()
    time.sleep(5)

    result = exec_in_container(container_name, command)
    time.sleep(5)

    stop_event.set()
    monitor_thread.join()

    metrics = result_holder[0]
    return metrics, result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Colibri Benchmark")
    parser.add_argument("--runs", type=int, default=None, help="Numero di iterazioni (sovrascrive config.yml)")
    
    ## Aggiunge i nuovi flag CLI accanto a --runs
    parser.add_argument("--scenario", type=str, default=None)
    parser.add_argument("--delay-ms", type=float, default=None)
    parser.add_argument("--jitter-ms", type=float, default=None)
    parser.add_argument("--loss-pct", type=float, default=None)
    parser.add_argument("--mode", type=str, default=None, choices=["split", "oneside"])
    parser.add_argument("--cipher", type=str, default=None, choices=["x25519", "mlkem512"])
    
    args = parser.parse_args()
    #---------------------------------------------------------------
    # LOAD CONFIGURATION FILE 
    #---------------------------------------------------------------
    print(f"[=] Parsing configuration file {CONF_FILE} ...");
    with open(CONF_FILE) as f:
        config = yaml.safe_load(f)
    # Se l'utente passa --runs, usiamo quello, altrimenti il default dal file
    ITERATIONS = args.runs if args.runs is not None else config["iterations"]
    RESULTS_DIR = config["results_dir"] 
    COMPOSE_FILE = config["compose_file"]
    LOG_INITIATOR = config["log_initiator"]
    CONNECTION_NAME = config["connection_name"]
    CONTAINER_RESPONDER = config["container_responder"]
    CONTAINER_INITIATOR = config["container_initiator"]

    if CONTAINER_INITIATOR == "initiator_minimal":
        CMD_UP = "./hummingbird" # definire in base al container
    else:
        exec_in_container(CONTAINER_INITIATOR, "swanctl --load-all --noprompt")
        exec_in_container(CONTAINER_INITIATOR, "swanctl --reload-settings")
        CMD_UP = f"swanctl --initiate --ike {CONNECTION_NAME}"

    # Il reset della connessione lo facciamo fare al responder in modo tale da evitare che questo vada ad impattare 
    # sulle misurazioni fatte per il responder anche se comunque viene fatta al di fuori de monitoring, inoltre serve
    # perchè l'initioator minimal non è ancora in grado di farlo
    CMD_DOWN = f"swanctl --terminate -f --ike {CONNECTION_NAME}"
    
    print(f"[+] Configuration settings loaded ...");
    #---------------------------------------------------------------
    # STARTING ENVIRONMENT
    #---------------------------------------------------------------
    if(is_docker_running() == False):
        start_docker_linux();
    print("[*] Docker is running...");
    docker_compose_up(COMPOSE_FILE);
    print("[*] The environment is running...");
    
    #---------------------------------------------------------------
    # NETWORK EMULATION (delay/jitter NTN) — applicata e VERIFICATA
    # PRIMA di qualunque handshake
    #---------------------------------------------------------------
    DELAY_MS   = args.delay_ms  if args.delay_ms  is not None else config.get("delay_ms", 0)
    JITTER_MS  = args.jitter_ms if args.jitter_ms is not None else config.get("jitter_ms", 0)
    LOSS_PCT   = args.loss_pct  if args.loss_pct  is not None else config.get("loss_pct", 0)
    DELAY_MODE = args.mode      if args.mode      is not None else config.get("delay_mode", "split")
    SCENARIO   = args.scenario  if args.scenario  is not None else config.get("network_scenario", "custom")
    RESPONDER_IP = config.get("responder_ip", "192.168.100.2")
    
    CIPHER_LABEL = args.cipher if args.cipher is not None else config.get("cipher_suite", "unknown")
    if CONTAINER_INITIATOR == "initiator_minimal" and CIPHER_LABEL != "unknown":
        conf_path = config.get("initiator_minimal_conf", "../env/initiator_minimal/conf.ini")
        with open(conf_path) as f:
            match = re.search(r"key-exchange\s*=\s*(\S+)", f.read())
        actual_cipher = match.group(1) if match else None
        if actual_cipher != CIPHER_LABEL:
            log_err(f"MISMATCH: hai chiesto --cipher {CIPHER_LABEL} ma {conf_path} ha "
                     f"key-exchange={actual_cipher}. Aggiorna il conf.ini prima di lanciare.")
            exit(1)
        log_ok(f"Cipher suite confermata: {CIPHER_LABEL}")

    log_info(f"=== Condizione: scenario={SCENARIO} cipher={CIPHER_LABEL} "
              f"delay={DELAY_MS}ms jitter={JITTER_MS}ms mode={DELAY_MODE} "
              f"loss={LOSS_PCT}% runs={ITERATIONS} ===")

    veth_initiator = get_veth(CONTAINER_INITIATOR)
    veth_responder = get_veth(CONTAINER_RESPONDER)
    if veth_initiator is None or veth_responder is None:
        log_err("Impossibile determinare il veth di uno dei due container. "
                "Verifica manualmente con: docker exec <container> cat /sys/class/net/eth0/iflink")
        exit(1)
    log_info(f"veth {CONTAINER_INITIATOR} -> {veth_initiator}")
    log_info(f"veth {CONTAINER_RESPONDER} -> {veth_responder}")
    
    baseline_ok, BASELINE_RTT_MS = measure_rtt(RESPONDER_IP, count=50, source_container=CONTAINER_INITIATOR)
    if not baseline_ok:
        log_err("Impossibile misurare l'RTT di baseline (senza netem).")
        exit(1)
    log_info(f"RTT di baseline (senza netem, container-to-container): {BASELINE_RTT_MS:.3f}ms")

    EFFECTIVE_DELAY_MS = max(0.0, DELAY_MS - BASELINE_RTT_MS)
    if BASELINE_RTT_MS >= DELAY_MS:
        log_err(f"ATTENZIONE: baseline ({BASELINE_RTT_MS:.3f}ms) >= target ({DELAY_MS}ms). "
                f"netem non aggiungerà alcun ritardo; la misura rifletterà solo l'overhead del testbed.")
    else:
        log_info(f"Delay netem compensato: applico {EFFECTIVE_DELAY_MS:.3f}ms invece di {DELAY_MS}ms "
                  f"(differenza già coperta dalla baseline)")

    if DELAY_MS > 0:
        if DELAY_MODE == "split":
            apply_netem(veth_initiator, EFFECTIVE_DELAY_MS / 2, JITTER_MS / 2)
            apply_netem(veth_responder, EFFECTIVE_DELAY_MS / 2, JITTER_MS / 2)
        elif DELAY_MODE == "oneside":
            apply_netem(veth_responder, EFFECTIVE_DELAY_MS, JITTER_MS)
        else:
            log_err(f"delay_mode sconosciuto: {DELAY_MODE}")
            exit(1)

        ok, measured = verify_rtt(RESPONDER_IP, DELAY_MS, source_container=CONTAINER_INITIATOR)
        if not ok:
            log_err("Verifica FALLITA: nessun handshake verrà eseguito con una condizione non verificata.")
            clear_netem(veth_initiator)
            clear_netem(veth_responder)
            exit(1)
    else:
        log_info("DELAY_MS=0, nessun netem applicato (baseline).")

    #---------------------------------------------------------------
    # STARTING SIMULATION
    #---------------------------------------------------------------
    try:
        for i in range(ITERATIONS):

            log_info(f"Iterations {i+1} of {ITERATIONS}")
            print("----------------------------------------------")

            metrics, output = run_single_iteration(CONTAINER_INITIATOR, CMD_UP)
            if CONTAINER_INITIATOR == "initiator_minimal":
                timings = parse_benchmark_output(output)
            else:
                timings = calcola_differenze(LOG_INITIATOR)
            timings_list.append(timings)
            
            metrics["init_duration"] = timings.get("init_duration")
            metrics["auth_duration"] = timings.get("auth_duration")
            metrics["total_duration"] = timings.get("total_duration")
            
            all_results.append(metrics)


            
            if CONTAINER_INITIATOR == "initiator_minimal":
                timings = parse_benchmark_output(output)
            else:
                timings = calcola_differenze(LOG_INITIATOR)
            timings_list.append(timings)

            exec_in_container(CONTAINER_RESPONDER, CMD_DOWN) 
            if CONTAINER_INITIATOR == "initiator_classic":
                exec_in_container(CONTAINER_INITIATOR, "swanctl --reload-settings")  

            time.sleep(2) 
            print("[✔] Environemnt Cleaned")
    finally:
        if DELAY_MS > 0:
            clear_netem(veth_initiator)
            clear_netem(veth_responder)

    
    print(timings_list)
      # --- Aggrega i risultati ---
    memory_peaks = [r["memory_peak"] for r in all_results]
    memory_avgs = [r["memory_avg"] for r in all_results]

    memory_summary = {
        "memory_avg_mean": statistics.mean(memory_avgs),
        "memory_avg_std": statistics.stdev(memory_avgs),
        "memory_peak_mean": statistics.mean(memory_peaks),
        "memory_peak_std": statistics.stdev(memory_peaks),
    }
    #docker_compose_down(compose_file=config["compose_file"]);

    init_values = sorted([t["init_duration"] for t in timings_list if t["init_duration"] is not None])
    auth_values = sorted([t["auth_duration"] for t in timings_list if t["auth_duration"] is not None])
    total_values = sorted([t["total_duration"] for t in timings_list if t["total_duration"] is not None])

    def get_detailed_stats(data):
        if not data: return None
        n = len(data)
        return {
            "min": min(data),
            "median": statistics.median(data),
            "p90": data[int(n * 0.90)],
            "p99": data[int(n * 0.99)],
            "max": max(data),
            "avg": statistics.mean(data)
        }

    time_summary = {
        "init": get_detailed_stats(init_values),
        "auth": get_detailed_stats(auth_values),
        "total": get_detailed_stats(total_values)
    }

    summary = {**memory_summary, **time_summary}

    print(summary)

    timestamp = int(time.time())
    os.makedirs("../results", exist_ok=True)


    CONDITION = {
        "cipher_suite": CIPHER_LABEL,
        "scenario": SCENARIO,
        "rtt_requested_ms": DELAY_MS,
        "baseline_rtt_ms": round(BASELINE_RTT_MS, 3),
        "netem_delay_applied_ms": round(EFFECTIVE_DELAY_MS, 3),
        "jitter_requested_ms": JITTER_MS,
        "loss_pct": LOSS_PCT,
        "delay_mode": DELAY_MODE,
        "runs": ITERATIONS,
        "container_initiator": CONTAINER_INITIATOR,
        "connection_name": CONNECTION_NAME,
    }

    def fmt_num(x):
        # 1.0 -> "1", 0.5 -> "0.5" — niente decimali inutili nel nome file
        return str(int(x)) if float(x).is_integer() else str(x)

    RESULT_PATH = (
        f"../results/{SCENARIO}_{CIPHER_LABEL}"
        f"_d{fmt_num(DELAY_MS)}ms_j{fmt_num(JITTER_MS)}ms"
        f"_l{fmt_num(LOSS_PCT)}pct_r{ITERATIONS}_{timestamp}.json"
    )

    save_benchmark_results(all_results, summary, condition=CONDITION, output_path=RESULT_PATH)
    print(f"[+] Benchmark saved in: {RESULT_PATH}")

