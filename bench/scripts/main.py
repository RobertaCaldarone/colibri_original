from utils.docker import ( is_docker_running, start_docker_linux, docker_compose_up, docker_compose_down, get_veth, exec_in_container)
from utils.monitoring import (get_mem_usage, monitor_container_resources)
from utils.save import (save_benchmark_results)
from utils.log import *
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
    # STARTING SIMULATION
    #---------------------------------------------------------------
    for i in range(ITERATIONS):

        log_info(f"Iterations {i+1} of {ITERATIONS}")
        print("----------------------------------------------")

        metrics, output = run_single_iteration(CONTAINER_INITIATOR, CMD_UP)
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
        "auth": get_detailed_stats(auth_values)
    }

    summary = {**memory_summary, **time_summary}

    print(summary)

    timestamp = int(time.time())
    os.makedirs("../results", exist_ok=True)



    RESULT_PATH = f"../results/{timestamp}_{CONTAINER_INITIATOR}_{CONNECTION_NAME}_runs{ITERATIONS}.json"

    save_benchmark_results(all_results, summary, output_path=RESULT_PATH)
    print(f"[+] Benchmark saved in: {RESULT_PATH}")

